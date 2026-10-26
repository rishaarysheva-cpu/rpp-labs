import unittest
from datetime import datetime, timedelta, timezone
from library_app.web import create_app
from library_app.domain import (Loan, Book, BookTitle, ISBN, Money, LoanPeriod,
    InvalidValueObject, DomainInvariantViolation, TransitionNotAllowed, ClockMovedBack,
    STATES, EVENTS, Created, GatewayUnresolved)
from library_app.application import three_working_days, xor_gateway

class FixedClock:
    def __init__(self): self.value = datetime(2030, 1, 4, 10, tzinfo=timezone.utc)  # пятница
    def now(self): return self.value
    def move_to(self, value):
        if value < self.value: raise ClockMovedBack('Нельзя назад')
        self.value = value

class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock()
        self.app = create_app(self.clock, seed=False)
        self.client = self.app.test_client()
        self.p = self.app.extensions['process']
        self.client.post('/books', json=dict(id='b', title='Книга', isbn='9785699120147', total_copies=1))
        for i in range(4): self.client.post('/readers', json=dict(id=f'r{i}', name=f'Читатель {i}'))
    def post(self, url, data=None, code=200):
        r = self.client.post(url, json=data)
        self.assertEqual(r.status_code, code, r.json)
        return r.json
    def reserve(self, reader='r0'):
        return self.post('/loans', dict(book_id='b', reader_id=reader), 201)['id']
    def operation(self, lid, op, data=None, code=200): return self.post(f'/loans/{lid}/{op}', data, code)
    def state(self, lid): return self.client.get(f'/loans/{lid}').json
    def finish(self, lid, damaged=False):
        self.operation(lid, 'return')
        self.operation(lid, 'inspect', dict(damaged=damaged, act_id='ACT-1'))
        self.operation(lid, 'assess-fine')
        self.operation(lid, 'release-stock')
        return self.operation(lid, 'close')
    def test_main_path(self):
        lid = self.reserve();self.operation(lid, 'issue')
        self.assertEqual(self.finish(lid)['state_class'], 'Closed')
        self.assertEqual(self.p.books.get('b').available_copies, 1)
        self.assertEqual(self.p.readers.get('r0').debt.amount, 0)
    def test_test_client_context_can_be_cleaned_up_twice(self):
        with self.app.test_client() as client:
            self.assertEqual(client.get('/health').status_code, 200)
            self.assertEqual(client.get('/catalog').status_code, 200)
        self.assertEqual(self.client.get('/health').status_code, 200)
    def test_debt_rejection_and_payment(self):
        self.p.add_reader(dict(id='debtor', name='Должник', debt='30'))
        lid=self.reserve('debtor')
        self.assertEqual(self.operation(lid,'issue')['state_class'],'Rejected')
        self.assertEqual(self.p.books.get('b').available_copies,1)
        self.post('/readers/debtor/pay-fine',dict(amount='30'))
        self.assertEqual(self.p.readers.get('debtor').debt.amount,0)
    def test_expiry_at_exact_boundary_and_queue(self):
        lid=self.reserve();q=self.reserve('r1')
        until=datetime.fromisoformat(self.state(lid)['reserved_until'])
        self.assertEqual(until,self.clock.now()+timedelta(days=5))
        self.post('/internal/tick',dict(now=(until-timedelta(microseconds=1)).isoformat()))
        self.assertEqual(self.state(lid)['state_class'],'Reserved')
        self.post('/internal/tick',dict(now=until.isoformat()))
        self.assertEqual(self.state(lid)['state_class'],'Expired')
        self.assertEqual(self.state(q)['state_class'],'Reserved')
        self.assertEqual(self.p.books.get('b').available_copies,0)
    def test_expiry_without_queue(self):
        lid=self.reserve();self.post('/internal/tick',dict(now=self.state(lid)['reserved_until']))
        self.assertEqual(self.p.books.get('b').available_copies,1)
    def test_reminder_idempotent_and_renew_once(self):
        lid=self.reserve();issued=self.operation(lid,'issue')
        at=datetime.fromisoformat(issued['due_at'])-timedelta(days=2)
        self.post('/internal/tick',dict(now=at.isoformat()))
        self.post('/internal/tick',{})
        self.assertEqual(sum(e['name']=='remind' for e in self.state(lid)['events']),1)
        old=self.state(lid)['due_at'];self.operation(lid,'renew')
        self.assertEqual(datetime.fromisoformat(self.state(lid)['due_at']),datetime.fromisoformat(old)+timedelta(days=7))
        before=self.state(lid);self.operation(lid,'renew')
        self.assertEqual(self.state(lid)['due_at'],before['due_at'])
        self.assertEqual(self.state(lid)['events'][-1]['name'],'renewal_denied')
    def test_renewal_denied_when_queue(self):
        lid=self.reserve();self.operation(lid,'issue');self.reserve('r1')
        before=self.state(lid);self.operation(lid,'renew');self.assertEqual(self.state(lid)['due_at'],before['due_at'])
        self.assertEqual(self.state(lid)['events'][-1]['name'],'renewal_denied')
    def test_renewal_rearms_reminder_overdue_and_escalation_deadlines(self):
        lid=self.reserve();issued=self.operation(lid,'issue')
        old_due=datetime.fromisoformat(issued['due_at'])
        self.post('/internal/tick',dict(now=(old_due-timedelta(days=2)).isoformat()))
        renewed=self.operation(lid,'renew');new_due=datetime.fromisoformat(renewed['due_at'])
        self.assertFalse(renewed['reminded'])
        self.post('/internal/tick',dict(now=(old_due+timedelta(days=1)).isoformat()))
        self.assertEqual(self.state(lid)['state_class'],'Issued')
        self.assertEqual(sum(e['name']=='remind' for e in self.state(lid)['events']),1)
        self.post('/internal/tick',dict(now=(new_due-timedelta(days=2)).isoformat()))
        self.post('/internal/tick',{})
        self.assertEqual(sum(e['name']=='remind' for e in self.state(lid)['events']),2)
        overdue_at=(new_due+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        self.post('/internal/tick',dict(now=overdue_at.isoformat()))
        self.assertEqual(self.state(lid)['state_class'],'Overdue')
        escalation=(new_due+timedelta(days=30)).replace(hour=0,minute=0,second=0,microsecond=0)
        self.post('/internal/tick',dict(now=(escalation-timedelta(microseconds=1)).isoformat()))
        self.assertEqual(self.state(lid)['state_class'],'Overdue')
        self.post('/internal/tick',dict(now=escalation.isoformat()))
        self.assertEqual(self.state(lid)['state_class'],'Escalated')
    def test_overdue_damage_or_join(self):
        lid=self.reserve();d=self.operation(lid,'issue')
        self.post('/internal/tick',dict(now=(datetime.fromisoformat(d['due_at'])+timedelta(days=2)).isoformat()))
        self.assertEqual(self.state(lid)['state_class'],'Overdue')
        result=self.finish(lid,True)
        self.assertEqual((result['late_fine'],result['damage_fine'],result['fine']),('30.00','500.00','530.00'))
        self.assertEqual(str(self.p.readers.get('r0').debt.amount),'530.00')
    def test_single_fine_branches(self):
        for damage,late,expected in [(True,0,'500.00'),(False,2,'30.00')]:
            with self.subTest(damage=damage,late=late):
                self.setUp();lid=self.reserve();d=self.operation(lid,'issue')
                if late:self.post('/internal/tick',dict(now=(datetime.fromisoformat(d['due_at'])+timedelta(days=late)).isoformat()))
                self.assertEqual(self.finish(lid,damage)['fine'],expected)
    def test_and_join_requires_all_branches(self):
        lid=self.reserve();self.operation(lid,'issue');self.operation(lid,'return')
        self.operation(lid,'close',code=422)
        self.operation(lid,'inspect',dict(damaged=False));self.operation(lid,'assess-fine')
        self.operation(lid,'close',code=422)
        self.operation(lid,'release-stock');self.operation(lid,'close')
        self.assertEqual(self.state(lid)['state_class'],'Closed')
    def test_parallel_order_is_independent(self):
        lid=self.reserve();self.operation(lid,'issue');self.operation(lid,'return')
        self.operation(lid,'release-stock');self.operation(lid,'inspect',dict(damaged=False))
        self.operation(lid,'assess-fine');self.operation(lid,'close')
    def test_three_invalid_events_keep_full_snapshot(self):
        lid=self.reserve()
        before=self.state(lid);self.operation(lid,'return',code=409);self.assertEqual(self.state(lid),before)
        self.operation(lid,'issue');before=self.state(lid)
        self.operation(lid,'issue',code=409);self.assertEqual(self.state(lid),before)
        self.finish(lid);before=self.state(lid)
        self.operation(lid,'renew',code=409);self.assertEqual(self.state(lid),before)
    def test_escalation_at_day_30(self):
        lid=self.reserve();d=self.operation(lid,'issue');due=datetime.fromisoformat(d['due_at'])
        self.post('/internal/tick',dict(now=(due+timedelta(days=29)).isoformat()))
        self.assertFalse(self.p.readers.get('r0').blocked)
        self.post('/internal/tick',dict(now=(due+timedelta(days=30)).isoformat()))
        self.assertEqual(self.state(lid)['state_class'],'Escalated')
        self.assertTrue(self.p.readers.get('r0').blocked)
        self.operation(lid,'return',code=409)
    def test_overdue_reader_rejected_even_without_tick(self):
        lid=self.reserve();d=self.operation(lid,'issue')
        self.p.add_book(dict(id='b2',title='Книга2',isbn='9785699120147',total_copies=1))
        self.clock.move_to(datetime.fromisoformat(d['due_at'])+timedelta(days=1))
        other=self.post('/loans',dict(book_id='b2',reader_id='r0'),201)['id']
        self.assertEqual(self.operation(other,'issue')['state_class'],'Rejected')
    def test_invalid_payload_and_missing_entity(self):
        self.post('/books',dict(title='',isbn='123',total_copies=-1),422)
        self.post('/internal/tick',dict(now='invalid'),422)
        self.post('/internal/tick',dict(now='2020-01-01T00:00:00+00:00'),422)
        self.operation('missing','issue',code=404)
        self.assertEqual(self.client.get('/loans/missing').status_code,404)
        self.post('/loans',dict(book_id=[],reader_id='r0'),422)
    def test_repeated_inspection_and_bad_act_leave_data_unchanged(self):
        lid=self.reserve();self.operation(lid,'issue');self.operation(lid,'return');before=self.state(lid)
        self.operation(lid,'inspect',dict(damaged=True),422);self.assertEqual(self.state(lid),before)
        self.operation(lid,'inspect',dict(damaged=False));before=self.state(lid)
        self.operation(lid,'inspect',dict(damaged=True,act_id='ACT-2'),422);self.assertEqual(self.state(lid),before)
    def test_fifo_queue(self):
        lid=self.reserve();q1=self.reserve('r1');q2=self.reserve('r2')
        self.post('/internal/tick',dict(now=self.state(lid)['reserved_until']))
        self.assertEqual(self.state(q1)['state_class'],'Reserved')
        self.assertEqual(self.state(q2)['state_class'],'Queued')
    def test_invariants_on_reconstitution(self):
        with self.assertRaises(DomainInvariantViolation):
            Book.reconstitute(dict(id='b',title='Книга',isbn='9785699120147',total_copies=1,available_copies=2))
        lid=self.reserve();self.operation(lid,'issue');self.finish(lid)
        d=self.state(lid);d['fine']=None
        with self.assertRaises(DomainInvariantViolation):Loan.reconstitute(d)
    def test_status_read_only_and_forbidden_state_events(self):
        loan=Loan('l','b','r',self.clock.now())
        with self.assertRaises(AttributeError):loan.status='Выдана'
        args={'queue':(), 'reserve':(self.clock.now(),self.clock.now()+timedelta(days=3)),
              'issue':(self.clock.now(),True),'reject':(self.clock.now(),),'expire':(self.clock.now(),),
              'remind':(self.clock.now(),),'renew':(self.clock.now(),False),'overdue':(self.clock.now(),),
              'return_book':(self.clock.now(),),'inspect':(False,None),'assess':(self.p.policy,),
              'release_stock':(), 'close':(),'escalate':(self.clock.now(),)}
        for cls in STATES.values():
            for event in EVENTS:
                if event not in cls.allowed:
                    with self.subTest(state=cls.__name__,event=event):
                        with self.assertRaises(TransitionNotAllowed):getattr(cls(),event)(loan,*args[event])
    def test_gateway_unresolved(self):
        with self.assertRaises(GatewayUnresolved):xor_gateway('Тест',[('a',False),('b',False)])
        with self.assertRaises(GatewayUnresolved):xor_gateway('Тест',[('a',True),('b',True)])
    def test_calendar_due_day_has_no_fine(self):
        lid=self.reserve();d=self.operation(lid,'issue')
        due=datetime.fromisoformat(d['due_at']).replace(hour=23,minute=59)
        self.post('/internal/tick',dict(now=due.isoformat()))
        self.assertEqual(self.state(lid)['state_class'],'Issued')
        self.assertEqual(self.finish(lid)['fine'],'0.00')
    def test_malformed_json(self):
        r=self.client.post('/loans',data='bad',content_type='application/json')
        self.assertEqual(r.status_code,422)
        self.assertIn('error',r.json)
    def test_closed_return_not_double_counted(self):
        lid=self.reserve();self.operation(lid,'issue');self.finish(lid)
        self.operation(lid,'release-stock',code=409)
        self.assertEqual(self.p.books.get('b').available_copies,1)

if __name__=='__main__':unittest.main()
