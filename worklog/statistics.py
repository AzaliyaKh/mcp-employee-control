from datetime import date, datetime, time, timedelta, timezone


LOCAL_ZONE = timezone(timedelta(hours=5))


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Ожидается дата ISO 8601 с часовым поясом')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('У даты отсутствует часовой пояс: ' + value)
    return result


def statistics(tasks, employee, start, end):
    left = datetime.combine(date.fromisoformat(start), time.min, LOCAL_ZONE)
    right = datetime.combine(date.fromisoformat(end) + timedelta(days=1), time.min, LOCAL_ZONE)
    if left >= right:
        raise ValueError('Начало периода позже конца')
    right = min(right, datetime.now(LOCAL_ZONE))
    if left >= right:
        raise ValueError('Период ещё не наступил')
    if not isinstance(tasks, list):
        raise ValueError('Ожидается JSON-массив нормализованных задач')
    result = {'employee': employee, 'start': start, 'end': end,
              'as_of': right.isoformat(), 'timezone': 'Asia/Yekaterinburg',
              'assigned': [], 'completed': [], 'overdue': [], 'current': [],
              'unavailable': [], 'warnings': []}
    keys = set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get('key'), str):
            raise ValueError('У задачи отсутствует строковый key')
        key = task['key']
        if key in keys:
            raise ValueError('Повторный ключ задачи: ' + key)
        keys.add(key)
        if task.get('history_complete') is not True:
            result['unavailable'].append(key)
            result['warnings'].append(f'{key}: неполная история, показатели не рассчитаны')
            continue
        created = timestamp(task['created_at'])
        known_from = timestamp(task.get('history_valid_from', task['created_at']))
        if known_from < created:
            raise ValueError(f'{key}: подтверждённая история раньше создания задачи')
        if known_from > created and max(created, left) < min(known_from, right):
            result['unavailable'].append(key)
            result['warnings'].append(f'{key}: период затрагивает промежуток до регистрации создания в истории; показатели не рассчитаны')
            continue
        events = sorted(task['events'], key=lambda event: timestamp(event['at']))
        assigned, status, deadline, completed_at = None, 'open', None, None
        completed_by = None
        included, first_breach, cursor = False, None, created

        def interval(until):
            nonlocal included, first_breach
            if until <= cursor:
                return
            if assigned == employee and cursor < right and until > left:
                included = True
            if (first_breach is None and deadline is not None
                    and status != 'completed' and max(cursor, deadline) < until):
                first_breach = (max(cursor, deadline), assigned)

        for event in events:
            at = timestamp(event['at'])
            if at < created:
                raise ValueError(f'{key}: событие раньше создания задачи')
            if at >= right:
                break
            interval(at)
            cursor = at
            if event['type'] == 'assignment':
                assigned = event['employee']
            elif event['type'] == 'status':
                status = event['value']
                if status not in ('open', 'in_progress', 'completed'):
                    raise ValueError(f'{key}: неизвестный статус')
                completed_at = at if status == 'completed' else None
                completed_by = assigned if status == 'completed' else None
            elif event['type'] == 'deadline':
                deadline = timestamp(event['value']) if event['value'] else None
            else:
                raise ValueError(f'{key}: неизвестный тип события')
        interval(right)
        if not included:
            continue
        result['assigned'].append(key)
        if status == 'completed' and completed_at and left <= completed_at < right and completed_by == employee:
            result['completed'].append(key)
        if status != 'completed' and assigned == employee:
            result['current'].append(key)
        if first_breach and left <= first_breach[0] < right and first_breach[1] == employee:
            result['overdue'].append(key)
    result['counts'] = {field: len(result[field]) for field in (
        'assigned', 'completed', 'overdue', 'current')}
    titles = {task['key']: task.get('title') or task['key'] for task in tasks}
    for category in ('completed', 'current'):
        result[category + '_tasks'] = [{'key': key, 'title': titles[key]} for key in result[category]]
    result['partial'] = bool(result['unavailable'])
    return result
