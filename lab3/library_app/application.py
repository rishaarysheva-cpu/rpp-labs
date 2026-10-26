"""Прикладной сервис и предикаты шлюзов. Flask здесь не используется."""
from datetime import timedelta
from uuid import uuid4
from .domain import (Book, Reader, Loan, BookTitle, ISBN, Money, Clock, BookRepository,
    LoanRepository, ReaderRepository, NotificationPort, PaymentPort,
    DomainInvariantViolation, GatewayUnresolved, EntityNotFound)

def has_available_copy(book): return book.available_copies > 0
def no_available_copy(book): return book.available_copies == 0
def reader_is_eligible(reader, overdue): return not reader.blocked and reader.debt.amount == 0 and not overdue
def reader_is_ineligible(reader, overdue): return reader.blocked or reader.debt.amount > 0 or overdue
def renewal_is_allowed(loan, has_queue, now): return not loan.renewed and not has_queue and now.date() <= loan.due_at.date()
def renewal_is_denied(loan, has_queue, now): return not renewal_is_allowed(loan, has_queue, now)
def has_overdue_fine(loan): return loan.returned_on > loan.period.due_on
def has_damage_fine(loan): return loan.damaged
def no_fine_needed(loan): return not has_overdue_fine(loan) and not has_damage_fine(loan)
def all_return_branches_completed(loan): return loan.stock_released and loan.inspected and loan.assessed
def queue_exists(loans, book_id): return any(x.book_id == book_id and x.state_class == 'Queued' for x in loans)
def queue_empty(loans, book_id): return not queue_exists(loans, book_id)

def xor_gateway(name, alternatives):
    selected = [label for label, condition in alternatives if condition]
    if len(selected) != 1:
        raise GatewayUnresolved(f'Шлюз {name}: требуется ровно одна ветвь, выбрано {len(selected)}')
    return selected[0]

def inclusive_fine_gateway(loan):
    branches = []
    if has_overdue_fine(loan): branches.append('late')
    if has_damage_fine(loan): branches.append('damage')
    if no_fine_needed(loan): branches.append('zero')
    if not branches: raise GatewayUnresolved('Не выбран исход расчёта штрафа')
    return branches

def three_working_days(moment):
    """Три дня Пн–Пт, день создания не считается, время суток сохраняется."""
    end, count = moment, 0
    while count < 3:
        end += timedelta(days=1)
        if end.weekday() < 5: count += 1
    return end

class LendingProcess:
    def __init__(self, books: BookRepository, loans: LoanRepository, readers: ReaderRepository,
                 clock: Clock, policy, notifications: NotificationPort, payments: PaymentPort):
        self.books, self.loans, self.readers = books, loans, readers
        self.clock, self.policy = clock, policy
        self.notifications, self.payments = notifications, payments
    def _save(self, loan, before):
        self.loans.save(loan)
        for event in loan.events[before:]: self.notifications.send(event)
    def add_book(self, data):
        book_id = data.get('id') or str(uuid4())
        if any(b.id == book_id for b in self.books.all()): raise DomainInvariantViolation('Книга уже существует')
        count = data.get('total_copies', 1)
        book = Book(book_id, BookTitle(data.get('title')), ISBN(data.get('isbn')), count, count)
        self.books.save(book)
        return book
    def add_reader(self, data):
        reader_id = data.get('id') or str(uuid4())
        if any(r.id == reader_id for r in self.readers.all()): raise DomainInvariantViolation('Читатель уже существует')
        reader = Reader(reader_id, data.get('name'), Money(data.get('debt', '0')))
        self.readers.save(reader)
        return reader
    def reserve_book(self, book_id, reader_id):
        book, reader = self.books.get(book_id), self.readers.get(reader_id)
        if any(x.book_id == book_id and x.reader_id == reader_id and not x.is_final for x in self.loans.all()):
            raise DomainInvariantViolation('У читателя уже есть активная заявка на эту книгу')
        now = self.clock.now()
        loan = Loan(str(uuid4()), book.id, reader.id, now)
        loan.record('reservation_requested', now, 'ИС')
        branch = xor_gateway('Есть свободный экземпляр', [('yes', has_available_copy(book)), ('no', no_available_copy(book))])
        if branch == 'yes':
            book.checkout_copy()
            loan.reserve(now, three_working_days(now))
        else: loan.queue(now)
        self.books.save(book)
        self._save(loan, 0)
        return loan
    def check_reader_debt(self, loan, now):
        reader = self.readers.get(loan.reader_id)
        overdue = any(x.reader_id == reader.id and x.is_open and now.date() > x.period.due_on
                      for x in self.loans.all())
        return reader, overdue
    def issue_book(self, loan_id):
        loan, now = self.loans.get(loan_id), self.clock.now()
        # Проверка события до проверки бизнес-условий сохраняет HTTP 409 для неверного состояния.
        if 'issue' not in loan.allowed_events: loan.issue(now, True)
        if now >= loan.reserved_until:
            raise DomainInvariantViolation('Срок брони истёк; вызовите /internal/tick')
        reader, overdue = self.check_reader_debt(loan, now)
        branch = xor_gateway('Можно выдать книгу', [('yes', reader_is_eligible(reader, overdue)),
                                                  ('no', reader_is_ineligible(reader, overdue))])
        n = len(loan.events)
        loan.record('reader_arrived', now, 'Библиотекарь')
        if branch == 'yes': loan.issue(now, True)
        else:
            loan.reject(now)
            book = self.books.get(loan.book_id)
            book.accept_return()
            self.books.save(book)
        self._save(loan, n)
        if branch == 'no': self.assign_next_reader(loan.book_id, now)
        return self.loans.get(loan_id)
    def renew_loan(self, loan_id):
        loan, now = self.loans.get(loan_id), self.clock.now()
        if 'renew' not in loan.allowed_events: loan.renew(now, False)
        waiting = queue_exists(self.loans.all(), loan.book_id)
        branch = xor_gateway('Можно продлить срок', [('yes', renewal_is_allowed(loan, waiting, now)),
                                                   ('no', renewal_is_denied(loan, waiting, now))])
        n = len(loan.events)
        if branch == 'yes': loan.renew(now, waiting)
        else: self.notify_renewal_denied(loan, now)
        self._save(loan, n)
        return loan
    def notify_renewal_denied(self, loan, now):
        loan.record('renewal_denied', now, loan.reader_id, (('reason', 'Очередь, повторное продление или просрочка'),))
    def receive_return(self, loan_id):
        loan, now = self.loans.get(loan_id), self.clock.now()
        n = len(loan.events)
        loan.return_book(now)
        self._save(loan, n)
        return loan
    def inspect_book(self, loan_id, damaged, act_id=None):
        loan, now = self.loans.get(loan_id), self.clock.now()
        n = len(loan.events)
        loan.inspect(now, damaged, act_id)
        if damaged: loan.record('damage_act_created', now, loan.reader_id, (('act_id', act_id),))
        self._save(loan, n)
        return loan
    def calculate_fine(self, loan_id):
        loan, now = self.loans.get(loan_id), self.clock.now()
        # Шлюз ИЛИ активирует просрочку, повреждение либо нулевой расчёт.
        if 'assess' not in loan.allowed_events: loan.assess(now, self.policy)
        if not loan.inspected: raise DomainInvariantViolation('Сначала зафиксируйте осмотр')
        branches = inclusive_fine_gateway(loan)
        n = len(loan.events)
        loan.assess(now, self.policy)
        loan.record('fine_branches_merged', now, loan.reader_id,
                    (('branches', ','.join(branches)), ('amount', str(loan.fine.amount))))
        self._save(loan, n)
        return loan
    def return_copy_to_stock(self, loan_id):
        loan, now = self.loans.get(loan_id), self.clock.now()
        n = len(loan.events)
        loan.mark_stock_released(now)
        book = self.books.get(loan.book_id)
        book.accept_return()
        self.books.save(book)
        self._save(loan, n)
        self.assign_next_reader(book.id, now)
        return loan
    def close_loan(self, loan_id):
        loan, now = self.loans.get(loan_id), self.clock.now()
        n = len(loan.events)
        loan.close(now)  # AND join внутри класса состояния.
        reader = self.readers.get(loan.reader_id)
        reader.add_debt(loan.fine)
        loan.record('fine_notified', now, reader.id, (('amount', str(loan.fine.amount)),))
        self.readers.save(reader)
        self._save(loan, n)
        return loan
    def assign_next_reader(self, book_id, now):
        book = self.books.get(book_id)
        waiting = sorted((x for x in self.loans.all() if x.book_id == book_id and x.state_class == 'Queued'),
                         key=lambda x: x.created_at)
        xor_gateway('Есть очередь', [('yes', queue_exists(waiting, book_id)), ('no', queue_empty(waiting, book_id))])
        while waiting and book.available_copies > 0:
            loan = waiting.pop(0)
            n = len(loan.events)
            book.checkout_copy()
            loan.reserve(now, three_working_days(now))
            self._save(loan, n)
        self.books.save(book)
    def pay_fine(self, reader_id, amount):
        reader = self.readers.get(reader_id)
        money = Money(amount)
        reader.pay(money)  # Валидация выполняется до внешнего вызова.
        if not self.payments.charge(reader_id, money):
            raise DomainInvariantViolation('Платёж отклонён')
        self.readers.save(reader)
        return reader
    def on_reservation_expired(self, loan, now):
        n = len(loan.events)
        loan.on_reservation_expired(now)
        book = self.books.get(loan.book_id)
        book.accept_return()
        self.books.save(book)
        self._save(loan, n)
        self.assign_next_reader(book.id, now)
    def on_reminder_due(self, loan, now):
        n = len(loan.events)
        loan.on_reminder_due(now)
        self._save(loan, n)
    def on_loan_overdue(self, loan, now):
        n = len(loan.events)
        loan.on_loan_overdue(now)
        self._save(loan, n)
    def on_escalation_due(self, loan, now):
        n = len(loan.events)
        loan.on_escalation_due(now)
        reader = self.readers.get(loan.reader_id)
        reader.block()
        self.readers.save(reader)
        self._save(loan, n)
    def on_tick(self):
        now = self.clock.now()
        # Обрабатываем вехи по времени, а не только последнее событие большого скачка.
        for initial in self.loans.all():
            loan = self.loans.get(initial.id)
            if loan.state_class == 'Reserved' and now >= loan.reserved_until:
                self.on_reservation_expired(loan, now)
            loan = self.loans.get(initial.id)
            if loan.state_class == 'Issued':
                reminder_at = loan.due_at - timedelta(days=2)
                if not loan.reminded and now >= reminder_at:
                    self.on_reminder_due(loan, reminder_at)
                    loan = self.loans.get(initial.id)
                if now.date() > loan.due_at.date():
                    overdue_at = (loan.due_at + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    self.on_loan_overdue(loan, overdue_at)
            loan = self.loans.get(initial.id)
            if loan.state_class == 'Overdue' and now.date() >= loan.due_at.date() + timedelta(days=30):
                self.on_escalation_due(loan, (loan.due_at + timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0))
        return now
