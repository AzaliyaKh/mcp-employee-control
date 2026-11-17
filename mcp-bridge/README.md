# Подключение Bionic к сервису через MCP

Рабочий режим с --enable-privacy предоставляет четыре функции: resolve_employee, get_employee_statistics (только локальные числа), analyze_employee_work (явно запрошенный облачный анализ) и privacy_decision (решение по показанной по названию задаче). У статистики нет отправки, маскировки и privacy-вопросов. Ниже сохранены инструкции демонстрации и история подключения.

Для реального Bitrix и Codex используйте [BITRIX_LIVE.md](../BITRIX_LIVE.md). Ниже — сохранённая демонстрация. В рабочем режиме добавляется аргумент --enable-privacy и третья функция privacy_decision; токен берётся из data/bitrix-live, а не lmstudio-demo. Общая HTTP-сводка теперь включает пояснение анализатора и безопасную причину ошибки, если анализ остановился.

Новый способ заменяет прежний Tools Provider. MCP используется только между Bionic и этим переходником. Сервис по-прежнему обращается к Bitrix/Kaiten/GitLab напрямую через API. Переходник предоставляет две фиксированные функции: resolve_employee и get_employee_statistics. Файлов, команд оболочки, произвольных URL и функций редактирования сотрудников среди инструментов нет.

Требуется Node.js 22.21 или новее. Установка npm-пакетов, SDK LM Studio и запуск lms dev больше не нужны. server.mjs реализует узкий набор MCP stdio: initialize, ping, tools/list, tools/call, progress и отмену ожидания. Полноценные resources/prompts/sampling и Streamable HTTP не реализуются и не объявляются.

## Демонстрация

В PowerShell из корня проекта:

```powershell
python tools/prepare_mcp_demo.py
python tools/start_lmstudio_demo.py
```

Первый шаг создаёт data/lmstudio-demo/mcp-entry.json с абсолютными путями к Node.js, переходнику и файлу технического токена. Самого токена в JSON нет. Второй запускает демонстрационный HTTP-сервис на 127.0.0.1:8000. Его окно оставить открытым. Используется отдельная база вымышленного Иванова, рабочий справочник не затрагивается.

В Bionic откройте Settings -> MCP и добавьте локальный сервер из mcp-entry.json. Для редактора mcp.json это объект employee-worklog внутри mcpServers. Если другие серверы уже настроены, добавьте одну запись, а не заменяйте весь файл. При наличии отдельных полей command/arguments перенесите их значения из сгенерированного JSON. Точные элементы редактора зависят от версии Bionic.

Пример структуры (настоящие абсолютные пути берите из сгенерированного файла):

```json
{
  "mcpServers": {
    "employee-worklog": {
      "command": "C:\\Program Files\\nodejs\\node.exe",
      "args": [
        "C:\\Users\\Azaliya\\coding\\ai\\mcp-bridge\\server.mjs",
        "--address", "http://127.0.0.1:8000",
        "--token-file", "C:\\Users\\Azaliya\\coding\\ai\\data\\lmstudio-demo\\local-token.txt"
      ]
    }
  }
}
```

Bionic сам запускает MCP-процесс. Не запускайте server.mjs вручную для общения: stdout занят протоколом, он не печатает приглашение ввода. Старое окно lms dev можно закрыть через Ctrl+C. Токен не вставляйте в сообщения чата.

После подключения должны появиться инструменты resolve_employee и get_employee_statistics. Напишите модели:

```text
Используй инструменты employee-worklog: найди Иванова и покажи
статистику за период с 2026-10-01 по 2026-10-31.
```

Ожидается: назначена 1, выполнена 1, просрочена 1, текущих 0. Нужны фактические вызовы инструментов, а не просто правдоподобный ответ модели. Progress отображается, если клиент передал progressToken; конкретный вид индикации зависит от Bionic. Отмена прекращает ожидание переходника, но уже созданный запрос может продолжить выполняться в сервисе. Полный отчёт не выдаётся модели автоматически; возвращаются сводка и локальная ссылка.

## Проверка

```powershell
python -m unittest discover -s tests -p test_mcp_bridge.py -v
node --check mcp-bridge/server.mjs
```

Тесты запускают настоящий MCP-процесс и настоящий локальный HTTP-сервис на вымышленных данных. Проверены handshake, схемы, поиск, статистика, progress, отмена и ошибки. Реальный вызов из интерфейса Bionic проверяется отдельно пользователем.

Протокол: [stdio](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports), [tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools). Настройка клиента: [LM Studio MCP](https://lmstudio.ai/docs/app/mcp).
