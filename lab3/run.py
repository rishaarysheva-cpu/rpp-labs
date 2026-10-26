"""Локальный запуск для демонстрации на Windows, macOS и Linux."""
import argparse
import os
from library_app.web import create_app


def main():
    parser = argparse.ArgumentParser(description='ЛР3 — библиотека')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=os.getenv('PORT', '5000'))
    parser.add_argument('--check', action='store_true', help='Проверить приложение без запуска сервера')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Порт должен быть от 1 до 65535')
    app = create_app()
    if args.check:
        with app.test_client() as client:
            assert client.get('/health').status_code == 200
            assert client.get('/catalog').status_code == 200
        print('OK: приложение готово; /health и /catalog возвращают HTTP 200')
        return
    print(f'Откройте http://{args.host}:{args.port} в браузере. Остановка: Ctrl+C.', flush=True)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
