# Этапы ручного тестирования

План относится к текущей реализации, а не ко всем целевым требованиям. Сначала выполнить на Windows, затем повторить основные проверки на Linux. Это инструкция: ручной прогон по ней ещё не выполнен. Последний автоматический прогон — 54 успешных теста на Windows.

Если доступа к внешним системам нет, использовать выбранный набор из `OFFLINE_TEST_PLAN.md` и генератор `tools/generate_demo_data.py`.

Все сотрудники и контакты в `examples/manual` вымышленные. Этапы 1–9 не требуют облака или настоящих учётных записей внешних систем. Не использовать рабочую базу сотрудников для этих проверок.

## 1. Подготовить отдельную папку проверки

Открыть PowerShell в корне проекта и выполнить:

```powershell
python --version
node --version
$manualRoot = Join-Path (Get-Location) ("data/manual-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $manualRoot
$db = Join-Path $manualRoot 'private/registry.sqlite'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
```

Сохранить значение `$manualRoot`: в остальных командах используется именно эта папка. Не запускать проверки без `--db`/`--data-dir`, иначе можно обратиться к обычной базе `data/private/registry.sqlite`.

**Ожидается:** Python запускается, создана новая папка проверки. Node нужен только для этапа 9. Другие файлы проекта не изменены.

## 2. Проверить импорт и просмотр сотрудников

```powershell
python -m employees --db $db import examples/manual/employees.csv
```

Первый раз ответить **нет**. Затем:

```powershell
python -m employees --db $db list
```

**Ожидается:** справочник пуст. Повторить импорт и ответить **да**, затем снова выполнить `list`.

**Ожидается:** ровно два сотрудника — `EMP_001` Тестов Алексей и `EMP_002` Тестов Борис.

Ещё раз импортировать тот же файл.

**Ожидается:** две существующие записи, ни одного нового сотрудника; данные не перезаписаны.

## 3. Проверить добавление, редактирование и дубликаты

```powershell
python -m employees --db $db add
```

Ввести ФИО «Васильев Сергей», ID Bitrix `1003`, логин GitLab `manual_c`. Остальные поля можно оставить пустыми.

**Ожидается:** сотрудник получает `EMP_003`; после закрытия терминала/повторного запуска команды остаётся в базе. Для нового терминала снова задать `$manualRoot` и `$db` из этапа 1.

```powershell
python -m employees --db $db edit EMP_003
python -m employees --db $db list --json
```

Изменить ФИО на «Васильев Сергей Петрович», другие значения оставить через Enter.

**Ожидается:** метка остаётся `EMP_003`, ID и логин сохранены, старое ФИО видно в `history`. Повторное редактирование с `-` в поле логина очищает его; ID Bitrix остаётся, поэтому запись допустима.

Проверить ошибочный импорт:

```powershell
python -m employees --db $db import examples/manual/conflict.csv
python -m employees --db $db list
```

**Ожидается:** конфликт ID `1001`, импорт не выполнен; даже первая новая строка не сохранена. В базе по-прежнему три сотрудника.

Дополнительно начать `add`, ввести только имя и нажать Ctrl+C. **Ожидается:** отмена без новой записи. Удаления сотрудников через команды нет.

## 4. Проверить обычную маскировку

```powershell
$cleanRun = Join-Path $manualRoot 'privacy-clean'
$outbound = Join-Path $manualRoot 'outbound.txt'
python -m privacy --db $db prepare examples/manual/task.txt --run-dir $cleanRun
python -m privacy inspect $cleanRun
python -m privacy export $cleanRun --output $outbound
Get-Content -Encoding UTF8 $outbound
```

**Ожидается:** статус `policy_checked`. Полное имя, логин, почта и телефон заменены на `EMP_001`. Не должно остаться «Тестов Алексей», `manual_a`, `alpha@example.test` и исходного номера. Слова остального текста сохранены.

Сохранить конверт для проверки состава данных:

```powershell
$handoff = Join-Path $manualRoot 'handoff.json'
python -m privacy export $cleanRun --bundle --output $handoff
Get-Content -Encoding UTF8 $handoff
```

**Ожидается:** есть текст с метками, версия, employee_labels и source_ids; нет ФИО, поля names, справочника или обратного маппинга. Каталог `$cleanRun` приватный — его нельзя передавать анализатору целиком.

## 5. Проверить остановку и решения пользователя

```powershell
$blockedRun = Join-Path $manualRoot 'privacy-blocked'
python -m privacy --db $db prepare examples/manual/blocked.txt --run-dir $blockedRun
$blocked = python -m privacy inspect $blockedRun | ConvertFrom-Json
$blocked
python -m privacy export $blockedRun --output "$manualRoot/blocked-outbound.txt"
```

**Ожидается:** `awaiting_privacy_decision`; показаны неоднозначная фамилия «Тестов» и неизвестная почта. Выгрузка завершается ошибкой, `blocked-outbound.txt` не создан. Без ответа пользователя разрешение не появляется.

Проверить «Продолжить»:

```powershell
python -m privacy decide $blockedRun continue --version $blocked.version
python -m privacy export $blockedRun --output "$manualRoot/approved-outbound.txt"
Get-Content -Encoding UTF8 "$manualRoot/approved-outbound.txt"
```

**Ожидается:** `user_approved_exception`; показанные оставшиеся данные сохранены как есть. Это только локальная выгрузка, реальной отправки в облако нет.

Для «Пропустить» создать другой запуск:

```powershell
$skipRun = Join-Path $manualRoot 'privacy-skip'
$skip = python -m privacy --db $db prepare examples/manual/blocked.txt --run-dir $skipRun | ConvertFrom-Json
python -m privacy decide $skipRun skip --version $skip.version
python -m privacy export $skipRun --output "$manualRoot/skipped-outbound.txt"
```

**Ожидается:** статус `skipped`, выгрузка запрещена, выходной файл не создан. Продолжение остальных задач проверяется при появлении общего сценария проверки нескольких задач; автономные privacy-команды пока не управляют таким списком.

«Очистить локальной Qwen» пока не реализовано. Сейчас можно исправить исходный текст и запустить `prepare` в новом каталоге, но это не следует отмечать как успешную проверку автоматической очистки.

## 6. Проверить восстановление имён

```powershell
$restored = Join-Path $manualRoot 'restored-report.json'
python -m privacy restore $cleanRun examples/manual/answer.json --json --output $restored
Get-Content -Encoding UTF8 $restored
```

**Ожидается:** корректный JSON, в summary вместо `EMP_001` — «Тестов Алексей».

```powershell
python -m privacy restore $cleanRun examples/manual/unknown-answer.txt --output "$manualRoot/unknown-report.txt"
```

**Ожидается:** ошибка неизвестной метки `EMP_999`; отчёт не создан. Повторное сохранение в существующий `$restored` должно завершиться ошибкой, не меняя готовый файл.

## 7. Проверить, что разрешение не подходит изменённому тексту

```powershell
$changedRun = Join-Path $manualRoot 'privacy-changed'
python -m privacy --db $db prepare examples/manual/task.txt --run-dir $changedRun
Add-Content -Encoding UTF8 "$changedRun/candidate.txt" 'Добавленные после проверки данные'
python -m privacy export $changedRun --output "$manualRoot/changed-outbound.txt"
```

**Ожидается:** сообщение об изменённом материале, выгрузка запрещена. Не править `$cleanRun`: он нужен как контрольный успешный пример.

## 8. Проверить статистику, отчёт, журнал и временные файлы

```powershell
$first = python -m worklog --data-dir $manualRoot --config examples/config.json start --employee EMP_001 --start 2026-10-01 --end 2026-10-31 | ConvertFrom-Json
$duplicate = python -m worklog --data-dir $manualRoot --config examples/config.json start --employee EMP_001 --start 2026-10-01 --end 2026-10-31 | ConvertFrom-Json
$first.run_id
$duplicate.run_id
$done = python -m worklog --data-dir $manualRoot process $first.run_id | ConvertFrom-Json
python -m worklog --data-dir $manualRoot report $done.report_id
Get-Content -Encoding UTF8 "$manualRoot/logs/$($first.run_id).jsonl"
Test-Path "$manualRoot/work/$($first.run_id)"
Test-Path "$manualRoot/reports/$($done.report_id)/report.json"
```

**Ожидается:**

- Два активных одинаковых запроса имеют один run_id.
- Статус `succeeded`; имя — «Тестов Алексей»; `demo: true`.
- Для демонстрационной истории: assigned=1, completed=1, overdue=1, current=0.
- В журнале указаны этапы и «этап завершился успешно».
- Проверка временной папки возвращает False, готового отчёта — True.

После завершения создать такой же запрос снова: он получает новый run_id. Обработать его и убедиться, что первый отчёт остался на месте. Не проверять неполную историю как точную статистику: при history_complete=false ожидаются partial и список unavailable.

## 9. Проверить HTTP-интерфейс и открытие отчёта

В первом терминале с тем же `$manualRoot`:

```powershell
$env:WORKLOG_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m worklog --data-dir $manualRoot --config examples/config.json serve --port 8000
```

Оставить сервер работающим. Во втором терминале задать тот же токен в `$token` локально; не добавлять его в отчёт об ошибке:

```powershell
$token = '<токен из первого терминала>'
$headers = @{ Authorization = "Bearer $token" }
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -Headers $headers
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/employees?query=EMP_001' -Headers $headers
$body = @{ kind='employee_statistics'; employee_id='EMP_001'; start='2026-10-01'; end='2026-10-31' } | ConvertTo-Json
$job = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/analysis' -Headers $headers -ContentType 'application/json' -Body $body
$state = Invoke-RestMethod -Uri "http://127.0.0.1:8000/analysis/$($job.run_id)" -Headers $headers
$state
```

Если статус queued/running, повторить последний GET до завершения.

**Ожидается:** health=ok; поиск возвращает имя и метку, а не все аккаунты; запрос выполняется в фоне, появляется summary и report_url. Открыть report_url в браузере — виден полный отчёт. Общего токена подключения в ссылке нет. Примерно через 10 минут старая ссылка перестаёт работать, но сам отчёт остаётся; новый GET статуса выдаёт новую ссылку.

Отправить `/health` без Authorization. **Ожидается:** HTTP 401. Добавить в тело `/analysis` поле model или source_file — **ожидается HTTP 400**, а не смена модели/источника. Попытка получить `/reports/../../private/registry.sqlite` должна быть отклонена.

Остановить сервер через Ctrl+C. Не считать аварийное убийство процесса проверкой готового восстановления: автоматическое восстановление running после сбоя пока не реализовано.

Клиент Node.js уже покрыт автоматической сквозной проверкой. Ручную проверку настоящего чата LM Studio проводить на этапе 11: самостоятельная работа HTTP ещё не доказывает установку плагина.

## 10. Повторить базовые проверки на Linux и в Docker

После успешных этапов Windows:

1. На Linux создать новую отдельную папку ручного прогона и повторить этапы 2–9. Использовать `python3`, Linux-пути и свою оболочку вместо PowerShell. Сами действия и ожидаемые результаты не меняются.
2. Проверить русский текст в терминале, сохранность базы между запусками и права записи в выбранный каталог.
3. Собрать Docker-образ. Сначала проверить `python -m employees --db /data/manual-linux/private/registry.sqlite list` внутри контейнера — использовать тестовый путь, не обычную базу.
4. Добавить тестового сотрудника в эту базу, остановить и пересоздать контейнер, проверить ту же запись.
5. Проверить локальные privacy-команды внутри контейнера с тестовыми файлами в подключённой папке.

**Ожидается:** одинаковое поведение на Windows/Linux; база сохраняется вне контейнера; отсутствуют ошибки CRLF/прав доступа. Текущий Compose — контейнер команд без сети, HTTP автоматически не запускается. Отдельный образ анализатора не считается проверенной сетевой изоляцией только по факту успешной сборки.

## 11. Проверки, требующие настройки и дальнейшей реализации

| Проверка | Условие начала | Что проверить |
|---|---|---|
| Настоящий плагин LM Studio | Сборка с SDK и регистрация Tools Provider | Поиск сотрудника, уточнение одинаковых фамилий, «В процессе», сводка и ссылка; отсутствие токена в ответе модели |
| Bitrix | Адрес webhook и тестовая задача | Задача и все результаты отдельно от чата; несколько результатов; пустой результат; недоступная задача; пагинация |
| Kaiten | Тестовая карточка и точное место хранения отчёта | При корректном свойстве — отчёт; без настройки — not_configured; не угадывать отчёт по чату |
| GitLab | Тестовый проект и почты автора в справочнике | Только нужный автор и период; чужой коммит исключён; too_large/collapsed отмечены; нет токена в журнале |
| Текстовый анализатор | Выбранный разработчиком провайдер, тестовый ключ и ограниченное окружение | Только подготовленный конверт без маппинга; известные метки/источники; отказ при неверном ответе, без автоматической смены поставщика |
| Сравнение кода и отчёта | Реализация task_review/project_review | Сходится/частично/не подтверждено, параллельные задачи, отсутствие/нечитаемость отчёта, большой объём кода |
| «Очистить» через Qwen | Подключение действия к LM Studio | Фрагмент обрабатывается локально, затем повторная проверка скриптом; остаток снова требует решения |
| Codex CLI / Claude Code | Реализованный runner и проверенная изоляция | Нет доступа к справочнику, токенам источников и соседним запросам; разрешён только подготовленный ввод и выбранный провайдер |

Эти пункты не отмечать как пройденные по локальным примерам. Полный автоматический сценарий пока нельзя принять как готовый.

## Как записывать результаты

Для каждого этапа сохранить: дату, ОС, версию Python/Node, папку ручного прогона, действие, ожидаемый результат, фактический результат и статус **пройден / ошибка / не проверен / ещё не реализован**. При ошибке приложить этап и текст сообщения без ключей, токенов и настоящих персональных данных.

Первый приоритет сейчас: этапы 1–9 на Windows. Следующий — повтор на Linux/Docker. После настройки и доработок — этап 11.
