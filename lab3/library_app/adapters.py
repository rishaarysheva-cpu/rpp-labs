"""Инфраструктура: репозитории памяти, часы со сдвигом и внешние заглушки."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from .domain import Book, Loan, Reader, ClockMovedBack, EntityNotFound, InvalidValueObject

class InMemoryRepository:
    entity = None
    def __init__(self): self._items = {}
    def get(self, entity_id):
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise InvalidValueObject('Идентификатор должен быть непустой строкой')
        if entity_id not in self._items: raise EntityNotFound(f'Сущность {entity_id} не найдена')
        return self.entity.reconstitute(deepcopy(self._items[entity_id]))
    def save(self, entity): self._items[entity.id] = deepcopy(entity.to_state())
    def all(self): return [self.get(key) for key in self._items]
class InMemoryBookRepository(InMemoryRepository): entity = Book
class InMemoryLoanRepository(InMemoryRepository): entity = Loan
class InMemoryReaderRepository(InMemoryRepository): entity = Reader

class ShiftedClock:
    def __init__(self): self._offset = timedelta(0)
    def now(self): return datetime.now(timezone.utc) + self._offset
    def move_to(self, moment):
        if moment.tzinfo is None: moment = moment.replace(tzinfo=timezone.utc)
        if moment < self.now(): raise ClockMovedBack('Перевод часов назад запрещён')
        self._offset = moment - datetime.now(timezone.utc)

class InMemoryNotifications:
    def __init__(self): self.messages = []
    def send(self, event): self.messages.append(event)
class InMemoryPayments:
    def __init__(self): self.charges = []
    def charge(self, reader_id, amount):
        self.charges.append((reader_id, amount))
        return True
