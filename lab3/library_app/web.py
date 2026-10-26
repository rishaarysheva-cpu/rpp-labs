"""Flask — только веб-адаптер. Сервис и репозитории создаются один раз."""
import logging
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal
from flask import Flask, jsonify, request, render_template, g
from werkzeug.exceptions import BadRequest, UnsupportedMediaType
from .domain import (DomainError, TransitionNotAllowed, DomainInvariantViolation,
    InvalidValueObject, EntityNotFound, GatewayUnresolved, ClockMovedBack, FinePolicy, Money)
from .application import LendingProcess
from .adapters import (InMemoryBookRepository, InMemoryLoanRepository, InMemoryReaderRepository,
    ShiftedClock, InMemoryNotifications, InMemoryPayments)

def create_app(clock=None, seed=True):
    app = Flask(__name__)
    app.json.ensure_ascii = False
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper())
    process = LendingProcess(InMemoryBookRepository(), InMemoryLoanRepository(), InMemoryReaderRepository(),
        clock or ShiftedClock(), FinePolicy(Money(os.getenv('FINE_PER_DAY', '15')),
        Money(os.getenv('DAMAGE_FINE', '500'))), InMemoryNotifications(), InMemoryPayments())
    app.extensions['process'] = process
    if seed:
        process.add_book(dict(id='book-1', title='Мастер и Маргарита', isbn='978-5-699-12014-7', total_copies=2))
        process.add_reader(dict(id='reader-1', name='Читатель 1'))
    lock = threading.RLock()
    @app.before_request
    def acquire_lock():
        lock.acquire()
        g.rpp_lock_acquired = True
    @app.teardown_request
    def release_lock(exc):
        # Flask может повторно завершать сохранённый тестовый контекст.
        if g.pop('rpp_lock_acquired', False):
            lock.release()
    def body():
        try: value = request.get_json()
        except (BadRequest, UnsupportedMediaType) as exc: raise InvalidValueObject('Ожидается корректный JSON') from exc
        if not isinstance(value, dict): raise InvalidValueObject('Ожидается JSON-объект')
        return value
    def required(d, key):
        if key not in d: raise InvalidValueObject(f'Отсутствует поле {key}')
        return d[key]
    def loan_json(loan): return jsonify(loan.to_state())
    @app.errorhandler(DomainError)
    def domain_error(exc):
        codes = {TransitionNotAllowed: 409, EntityNotFound: 404, GatewayUnresolved: 500,
                 DomainInvariantViolation: 422, InvalidValueObject: 422, ClockMovedBack: 422}
        code = codes.get(type(exc), 422)
        return jsonify(error=type(exc).__name__, message=str(exc)), code
    @app.get('/')
    def index(): return render_template('index.html')
    @app.get('/health')
    def health(): return jsonify(status='ok')
    @app.get('/catalog')
    def catalog(): return jsonify([b.to_state() for b in process.books.all()])
    @app.post('/books')
    def add_book(): return jsonify(process.add_book(body()).to_state()), 201
    @app.get('/books/<book_id>')
    def get_book(book_id): return jsonify(process.books.get(book_id).to_state())
    @app.post('/readers')
    def add_reader(): return jsonify(process.add_reader(body()).to_state()), 201
    @app.get('/readers')
    def readers(): return jsonify([r.to_state() for r in process.readers.all()])
    @app.get('/readers/<reader_id>')
    def get_reader(reader_id): return jsonify(process.readers.get(reader_id).to_state())
    @app.post('/readers/<reader_id>/pay-fine')
    def pay_fine(reader_id): return jsonify(process.pay_fine(reader_id, required(body(), 'amount')).to_state())
    @app.get('/loans')
    def loans(): return jsonify([x.to_state() for x in process.loans.all()])
    @app.post('/loans')
    def reserve():
        d = body()
        return loan_json(process.reserve_book(required(d, 'book_id'), required(d, 'reader_id'))), 201
    @app.get('/loans/<loan_id>')
    def get_loan(loan_id): return loan_json(process.loans.get(loan_id))
    @app.post('/loans/<loan_id>/issue')
    def issue(loan_id): return loan_json(process.issue_book(loan_id))
    @app.post('/loans/<loan_id>/renew')
    def renew(loan_id): return loan_json(process.renew_loan(loan_id))
    @app.post('/loans/<loan_id>/return')
    def receive_return(loan_id): return loan_json(process.receive_return(loan_id))
    @app.post('/loans/<loan_id>/inspect')
    def inspect(loan_id):
        d = body()
        return loan_json(process.inspect_book(loan_id, required(d, 'damaged'), d.get('act_id')))
    @app.post('/loans/<loan_id>/assess-fine')
    def assess(loan_id): return loan_json(process.calculate_fine(loan_id))
    @app.post('/loans/<loan_id>/release-stock')
    def release_stock(loan_id): return loan_json(process.return_copy_to_stock(loan_id))
    @app.post('/loans/<loan_id>/close')
    def close(loan_id): return loan_json(process.close_loan(loan_id))
    @app.get('/internal/clock')
    def time(): return jsonify(now=process.clock.now().isoformat())
    @app.post('/internal/tick')
    def tick():
        d = body() if request.data else {}
        if 'now' in d:
            if not isinstance(d['now'], str): raise InvalidValueObject('now должен быть строкой ISO 8601')
            try: moment = datetime.fromisoformat(d['now'].replace('Z', '+00:00'))
            except ValueError as exc: raise InvalidValueObject('Неверный формат ISO 8601') from exc
            if moment.tzinfo is None: moment = moment.replace(tzinfo=timezone.utc)
            process.clock.move_to(moment)
        now = process.on_tick()
        return jsonify(now=now.isoformat(), loans=[x.to_state() for x in process.loans.all()])
    return app
