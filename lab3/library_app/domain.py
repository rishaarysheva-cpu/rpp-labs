"""Домен библиотеки. Не зависит от Flask, ОС и текущего системного времени."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

class DomainError(Exception): pass
class DomainInvariantViolation(DomainError): pass
class InvalidValueObject(DomainError): pass
class EntityNotFound(DomainError): pass
class GatewayUnresolved(DomainError): pass
class ClockMovedBack(DomainError): pass
class TransitionNotAllowed(DomainError):
    def __init__(self, state, event):
        super().__init__(f'Событие {event} недопустимо в состоянии «{state}»')

@dataclass(frozen=True)
class BookTitle:
    value: str
    def __post_init__(self):
        if not isinstance(self.value, str) or not self.value.strip():
            raise InvalidValueObject('Название книги не может быть пустым')

@dataclass(frozen=True)
class ISBN:
    value: str
    def __post_init__(self):
        if not isinstance(self.value, str):
            raise InvalidValueObject('ISBN должен быть строкой')
        digits = self.value.replace('-', '').replace(' ', '')
        if len(digits) not in (10, 13) or not digits.isdigit():
            raise InvalidValueObject('ISBN должен содержать 10 или 13 цифр')

@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str = 'RUB'
    def __post_init__(self):
        try:
            value = Decimal(str(self.amount))
            if not value.is_finite() or value < 0:
                raise InvalidOperation
            value = value.quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise InvalidValueObject('Сумма должна быть конечной и неотрицательной') from exc
        if not isinstance(self.currency, str) or len(self.currency) != 3 or not self.currency.isalpha():
            raise InvalidValueObject('Код валюты должен состоять из трёх букв')
        object.__setattr__(self, 'amount', value)
        object.__setattr__(self, 'currency', self.currency.upper())

@dataclass(frozen=True)
class LoanPeriod:
    issued_on: date
    due_on: date
    def __post_init__(self):
        if not isinstance(self.issued_on, date) or not isinstance(self.due_on, date):
            raise InvalidValueObject('Даты периода должны иметь тип date')
        if self.due_on < self.issued_on:
            raise InvalidValueObject('Срок возврата раньше даты выдачи')

@dataclass(frozen=True)
class FinePolicy:
    per_day: Money
    damage: Money = Money(Decimal('500'))
    def __post_init__(self):
        if not isinstance(self.per_day, Money) or not isinstance(self.damage, Money):
            raise InvalidValueObject('Ставки должны иметь тип Money')
        if self.per_day.currency != self.damage.currency:
            raise InvalidValueObject('Валюты ставок различаются')
    def calculate(self, due_on, returned_on):
        return Money(self.per_day.amount * max(0, (returned_on - due_on).days), self.per_day.currency)

class Clock(Protocol):
    def now(self) -> datetime: ...

@dataclass(frozen=True)
class DomainEvent:
    loan_id: str
    name: str
    occurred_at: datetime
    recipient: str
    details: tuple[tuple[str, str], ...] = ()

class NotificationPort(Protocol):
    def send(self, event: DomainEvent) -> None: ...

class PaymentPort(Protocol):
    def charge(self, reader_id: str, amount: Money) -> bool: ...

class Book:
    def __init__(self, book_id, title, isbn, total_copies, available_copies):
        if not isinstance(book_id, str) or not book_id.strip():
            raise DomainInvariantViolation('Нужен идентификатор книги')
        if not isinstance(title, BookTitle) or not isinstance(isbn, ISBN):
            raise DomainInvariantViolation('Некорректные название или ISBN')
        if (type(total_copies) is not int or type(available_copies) is not int
                or not 0 <= available_copies <= total_copies):
            raise DomainInvariantViolation('Некорректное количество экземпляров')
        self._id, self.title, self.isbn = book_id, title, isbn
        self._total_copies, self._available_copies = total_copies, available_copies
    @property
    def id(self): return self._id
    @property
    def total_copies(self): return self._total_copies
    @property
    def available_copies(self): return self._available_copies
    def checkout_copy(self):
        if self.available_copies <= 0:
            raise DomainInvariantViolation('Нет свободных экземпляров')
        self._available_copies -= 1
    def accept_return(self):
        if self.available_copies >= self.total_copies:
            raise DomainInvariantViolation('Возврат превышает количество экземпляров')
        self._available_copies += 1
    def to_state(self):
        return dict(id=self.id, title=self.title.value, isbn=self.isbn.value,
                    total_copies=self.total_copies, available_copies=self.available_copies)
    @classmethod
    def reconstitute(cls, data):
        return cls(data['id'], BookTitle(data['title']), ISBN(data['isbn']),
                   data['total_copies'], data['available_copies'])

class Reader:
    def __init__(self, reader_id, name, debt=Money(Decimal('0')), blocked=False):
        if not isinstance(reader_id, str) or not reader_id.strip() or not isinstance(name, str) or not name.strip():
            raise InvalidValueObject('Нужны идентификатор и имя читателя')
        if type(blocked) is not bool or not isinstance(debt, Money):
            raise InvalidValueObject('Некорректные данные читателя')
        self.id, self.name, self.debt, self.blocked = reader_id, name, debt, blocked
    def add_debt(self, money): self.debt = Money(self.debt.amount + money.amount)
    def pay(self, money):
        if money.currency != self.debt.currency or money.amount <= 0 or money.amount > self.debt.amount:
            raise DomainInvariantViolation('Оплата должна быть положительной и не превышать долг')
        self.debt = Money(self.debt.amount - money.amount)
    def block(self): self.blocked = True
    def to_state(self):
        return dict(id=self.id, name=self.name, debt=str(self.debt.amount), blocked=self.blocked)
    @classmethod
    def reconstitute(cls, d): return cls(d['id'], d['name'], Money(d['debt']), d['blocked'])

class LoanState:
    name = ''
    allowed = frozenset()
    final = False
    def queue(self, loan): raise TransitionNotAllowed(self.name, 'queue')
    def reserve(self, loan, now, until): raise TransitionNotAllowed(self.name, 'reserve')
    def issue(self, loan, now, eligible): raise TransitionNotAllowed(self.name, 'issue')
    def reject(self, loan, now): raise TransitionNotAllowed(self.name, 'reject')
    def expire(self, loan, now): raise TransitionNotAllowed(self.name, 'expire')
    def remind(self, loan, now): raise TransitionNotAllowed(self.name, 'remind')
    def renew(self, loan, now, has_queue): raise TransitionNotAllowed(self.name, 'renew')
    def overdue(self, loan, now): raise TransitionNotAllowed(self.name, 'overdue')
    def return_book(self, loan, now): raise TransitionNotAllowed(self.name, 'return_book')
    def inspect(self, loan, damaged, act_id): raise TransitionNotAllowed(self.name, 'inspect')
    def assess(self, loan, policy): raise TransitionNotAllowed(self.name, 'assess')
    def release_stock(self, loan): raise TransitionNotAllowed(self.name, 'release_stock')
    def close(self, loan): raise TransitionNotAllowed(self.name, 'close')
    def escalate(self, loan, now): raise TransitionNotAllowed(self.name, 'escalate')

class Created(LoanState):
    name = 'Создана'; allowed = frozenset({'queue', 'reserve'})
    def queue(self, loan): return Queued()
    def reserve(self, loan, now, until):
        loan.reserved_until = until
        return Reserved()

class Queued(Created):
    name = 'Поставлена в очередь'; allowed = frozenset({'reserve'})
    def queue(self, loan): raise TransitionNotAllowed(self.name, 'queue')

class Reserved(LoanState):
    name = 'Забронирована'; allowed = frozenset({'issue', 'reject', 'expire'})
    def issue(self, loan, now, eligible):
        if now >= loan.reserved_until:
            raise DomainInvariantViolation('Срок брони истёк; обработайте таймер')
        if not eligible: raise DomainInvariantViolation('Читатель имеет задолженность или заблокирован')
        loan.issued_at = now
        loan.due_at = now + timedelta(days=14)
        loan.period = LoanPeriod(now.date(), loan.due_at.date())
        return Issued()
    def reject(self, loan, now): return Rejected()
    def expire(self, loan, now):
        if now < loan.reserved_until: raise DomainInvariantViolation('Срок брони ещё не истёк')
        return Expired()

class Issued(LoanState):
    name = 'Выдана'
    allowed = frozenset({'remind', 'renew', 'overdue', 'return_book'})
    def remind(self, loan, now):
        if loan.reminded or now < loan.due_at - timedelta(days=2):
            raise DomainInvariantViolation('Напоминание уже отправлено или ещё не наступило')
        loan.reminded = True
        return Issued()
    def renew(self, loan, now, has_queue):
        if has_queue or loan.renewed or now.date() > loan.due_at.date():
            raise DomainInvariantViolation('Продление недоступно: очередь, повторное продление или просрочка')
        loan.due_at += timedelta(days=7)
        loan.period = LoanPeriod(loan.issued_at.date(), loan.due_at.date())
        loan.renewed = True
        loan.reminded = False
        return Issued()
    def overdue(self, loan, now):
        if now.date() <= loan.due_at.date(): raise DomainInvariantViolation('Просрочка ещё не наступила')
        return Overdue()
    def return_book(self, loan, now):
        if now < loan.issued_at: raise DomainInvariantViolation('Возврат раньше выдачи')
        loan.returned_on = now.date()
        return Returned()

class Overdue(LoanState):
    name = 'Просрочена'; allowed = frozenset({'return_book', 'escalate'})
    def return_book(self, loan, now): return Issued.return_book(self, loan, now)
    def escalate(self, loan, now):
        if now.date() < loan.due_at.date() + timedelta(days=30):
            raise DomainInvariantViolation('Срок передачи дела ещё не наступил')
        return Escalated()

class Returned(LoanState):
    name = 'Возвращена'; allowed = frozenset({'inspect', 'assess', 'release_stock', 'close'})
    def inspect(self, loan, damaged, act_id):
        if loan.inspected: raise DomainInvariantViolation('Результат осмотра уже зафиксирован')
        if type(damaged) is not bool: raise InvalidValueObject('damaged должен быть boolean')
        if damaged and (not isinstance(act_id, str) or not act_id.strip()):
            raise DomainInvariantViolation('При повреждении нужен номер акта')
        loan.inspected, loan.damaged, loan.act_id = True, damaged, act_id if damaged else None
        return Returned()
    def assess(self, loan, policy):
        if not loan.inspected or loan.assessed:
            raise DomainInvariantViolation('Нужен осмотр; повторный расчёт запрещён')
        # Включающее ИЛИ: активируются обе ветви при одновременных причинах.
        loan.late_fine = policy.calculate(loan.period.due_on, loan.returned_on)
        loan.damage_fine = policy.damage if loan.damaged else Money(Decimal('0'))
        loan.fine = Money(loan.late_fine.amount + loan.damage_fine.amount)
        loan.assessed = True
        return Returned()
    def release_stock(self, loan):
        if loan.stock_released: raise DomainInvariantViolation('Экземпляр уже возвращён в фонд')
        loan.stock_released = True
        return Returned()
    def close(self, loan):
        # Слияние И: все ветви возврата должны закончиться.
        if not (loan.inspected and loan.assessed and loan.stock_released):
            raise DomainInvariantViolation('Не завершены осмотр, расчёт штрафа или возврат в фонд')
        return Closed()

class Closed(LoanState): name = 'Закрыта'; final = True
class Rejected(LoanState): name = 'Отклонена'; final = True
class Expired(LoanState): name = 'Бронь снята'; final = True
class Escalated(LoanState): name = 'Передана администрации'; final = True

STATES = {cls.__name__: cls for cls in (Created, Queued, Reserved, Issued, Overdue,
                                       Returned, Closed, Rejected, Expired, Escalated)}
EVENTS = ('queue', 'reserve', 'issue', 'reject', 'expire', 'remind', 'renew',
          'overdue', 'return_book', 'inspect', 'assess', 'release_stock', 'close', 'escalate')

class Loan:
    def __init__(self, loan_id, book_id, reader_id, created_at):
        if any(not isinstance(v, str) or not v.strip() for v in (loan_id, book_id, reader_id)):
            raise DomainInvariantViolation('Нужны идентификаторы выдачи, книги и читателя')
        self.id, self.book_id, self.reader_id = loan_id, book_id, reader_id
        self.created_at = created_at
        self._state = Created()
        self._events = []
        self.reserved_until = self.issued_at = self.due_at = self.period = self.returned_on = None
        self.fine = self.late_fine = self.damage_fine = None
        self.renewed = self.reminded = self.inspected = self.assessed = False
        self.damaged = self.stock_released = False
        self.act_id = None
    @property
    def status(self): return self._state.name
    @property
    def state_class(self): return type(self._state).__name__
    @property
    def allowed_events(self): return tuple(sorted(self._state.allowed))
    @property
    def events(self): return tuple(self._events)
    @property
    def is_final(self): return self._state.final
    @property
    def is_open(self): return self.state_class in ('Issued', 'Overdue')
    def _apply(self, method, now, recipient=None, details=(), *args):
        target = getattr(self._state, method)(self, *args)
        self._state = target
        self.record(method, now, recipient or self.reader_id, details)
    def record(self, name, now, recipient, details=()):
        self._events.append(DomainEvent(self.id, name, now, recipient, tuple(details)))
    def queue(self, now): self._apply('queue', now)
    def reserve(self, now, until): self._apply('reserve', now, None, (), now, until)
    def issue(self, now, eligible): self._apply('issue', now, None, (), now, eligible)
    def reject(self, now): self._apply('reject', now, None, (('reason', 'Задолженность или блокировка'),), now)
    def on_reservation_expired(self, now): self._apply('expire', now, None, (), now)
    def on_reminder_due(self, now): self._apply('remind', now, None, (), now)
    def renew(self, now, has_queue): self._apply('renew', now, None, (), now, has_queue)
    def on_loan_overdue(self, now): self._apply('overdue', now, None, (), now)
    def return_book(self, now): self._apply('return_book', now, None, (), now)
    def inspect(self, now, damaged, act_id): self._apply('inspect', now, None, (), damaged, act_id)
    def assess(self, now, policy): self._apply('assess', now, None, (), policy)
    def mark_stock_released(self, now): self._apply('release_stock', now, 'ИС')
    def close(self, now): self._apply('close', now)
    def on_escalation_due(self, now): self._apply('escalate', now, 'Администрация', (), now)
    def to_state(self):
        return dict(id=self.id, book_id=self.book_id, reader_id=self.reader_id,
            status=self.status, state_class=self.state_class, allowed_events=list(self.allowed_events),
            created_at=self.created_at.isoformat(), reserved_until=self.reserved_until.isoformat() if self.reserved_until else None,
            issued_at=self.issued_at.isoformat() if self.issued_at else None,
            due_at=self.due_at.isoformat() if self.due_at else None,
            returned_on=self.returned_on.isoformat() if self.returned_on else None,
            renewed=self.renewed, reminded=self.reminded, inspected=self.inspected, damaged=self.damaged,
            act_id=self.act_id, assessed=self.assessed, stock_released=self.stock_released,
            fine=str(self.fine.amount) if self.fine else None,
            late_fine=str(self.late_fine.amount) if self.late_fine else None,
            damage_fine=str(self.damage_fine.amount) if self.damage_fine else None,
            events=[dict(loan_id=e.loan_id, name=e.name, occurred_at=e.occurred_at.isoformat(),
                         recipient=e.recipient, details=dict(e.details)) for e in self.events])
    @classmethod
    def reconstitute(cls, d):
        loan = cls(d['id'], d['book_id'], d['reader_id'], datetime.fromisoformat(d['created_at']))
        if d['state_class'] not in STATES: raise DomainInvariantViolation('Неизвестный класс состояния')
        loan._state = STATES[d['state_class']]()
        for key in ('reserved_until', 'issued_at', 'due_at'):
            setattr(loan, key, datetime.fromisoformat(d[key]) if d[key] else None)
        loan.returned_on = date.fromisoformat(d['returned_on']) if d['returned_on'] else None
        for key in ('renewed', 'reminded', 'inspected', 'damaged', 'assessed', 'stock_released'):
            if type(d[key]) is not bool: raise DomainInvariantViolation('Некорректный признак')
            setattr(loan, key, d[key])
        loan.act_id = d['act_id']
        if loan.damaged:
            if not loan.inspected:
                raise DomainInvariantViolation('Повреждение должно быть подтверждено осмотром')
            if not isinstance(loan.act_id, str) or not loan.act_id.strip():
                raise DomainInvariantViolation('При повреждении нужен номер акта')
        for key in ('fine', 'late_fine', 'damage_fine'):
            setattr(loan, key, Money(d[key]) if d[key] is not None else None)
        if loan.state_class == 'Reserved' and (not loan.reserved_until or loan.reserved_until <= loan.created_at):
            raise DomainInvariantViolation('У брони должен быть корректный срок')
        if loan.state_class in ('Issued', 'Overdue', 'Returned', 'Closed', 'Escalated'):
            if not loan.issued_at or not loan.due_at: raise DomainInvariantViolation('Нет периода выдачи')
            loan.period = LoanPeriod(loan.issued_at.date(), loan.due_at.date())
        if loan.state_class in ('Returned', 'Closed'):
            if not loan.returned_on or loan.returned_on < loan.period.issued_on:
                raise DomainInvariantViolation('Некорректная дата возврата')
        if loan.assessed:
            if not loan.inspected or loan.fine is None or loan.late_fine is None or loan.damage_fine is None:
                raise DomainInvariantViolation('Нет данных расчёта штрафа')
            if loan.fine.amount != loan.late_fine.amount + loan.damage_fine.amount:
                raise DomainInvariantViolation('Некорректный итог штрафа')
        if loan.state_class == 'Closed' and not (loan.inspected and loan.assessed and loan.stock_released):
            raise DomainInvariantViolation('Выдача закрыта без завершения ветвей')
        loan._events = [DomainEvent(e['loan_id'], e['name'], datetime.fromisoformat(e['occurred_at']),
                                   e['recipient'], tuple(e['details'].items())) for e in d['events']]
        return loan

class BookRepository(Protocol):
    def get(self, book_id: str) -> Book: ...
    def save(self, book: Book) -> None: ...
    def all(self) -> list[Book]: ...
class LoanRepository(Protocol):
    def get(self, loan_id: str) -> Loan: ...
    def save(self, loan: Loan) -> None: ...
    def all(self) -> list[Loan]: ...
class ReaderRepository(Protocol):
    def get(self, reader_id: str) -> Reader: ...
    def save(self, reader: Reader) -> None: ...
    def all(self) -> list[Reader]: ...
