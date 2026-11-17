"""Deterministic text replacement; no network or model calls."""

import re


TEXT_FIELDS = ('full_name', 'aliases', 'gitlab_username', 'gitlab_emails',
               'emails', 'phones', 'logins')


def phone_pattern(value):
    digits = re.sub(r'\D', '', value)
    if len(digits) < 7:
        return None
    separator = r'[\s().-]*'
    parts = list(digits)
    if len(digits) == 11 and digits[0] in '78':
        parts[0] = '[78]'
    return r'(?<![\w+])\+?' + separator.join(parts) + r'(?!\w)'


def prepare_text(text, employees):
    reserved = []
    def escape_existing_label(match):
        reserved.append(match[0])
        return 'LITERAL_' + match[0]
    text = re.sub(r'(?<!\w)EMP_[0-9]+(?!\w)', escape_existing_label, text)
    terms = {}
    lengths = {}
    names = {}
    for employee in employees:
        label = employee['label']
        names[label] = employee['full_name']
        for field in TEXT_FIELDS:
            current = employee[field]
            values = current if isinstance(current, list) else [current]
            values = values + employee.get('history', {}).get(field, [])
            for value in values:
                if value.strip():
                    token = (phone_pattern(value) if field == 'phones' else None)
                    token = token or rf'(?<!\w){re.escape(value.casefold())}(?!\w)'
                    terms.setdefault(token, set()).add(label)
                    lengths[token] = max(lengths.get(token, 0), len(value))
    ordered = sorted(terms, key=lambda token: lengths[token], reverse=True)
    used = set()
    findings = []
    if ordered:
        pattern = re.compile('|'.join(
            f'(?P<t{index}>{token})'
            for index, token in enumerate(ordered)), re.IGNORECASE)

        def replace(match):
            index = int(match.lastgroup[1:])
            owners = terms[ordered[index]]
            if len(owners) != 1:
                return match[0]
            label = next(iter(owners))
            used.add(label)
            return label

        text = pattern.sub(replace, text)
        # A separate pass verifies what actually remains after replacement.
        for match in pattern.finditer(text):
            owners = terms[ordered[int(match.lastgroup[1:])]]
            findings.append({
                'kind': 'ambiguous_identity' if len(owners) > 1 else 'known_identity',
                'value': match[0], 'start': match.start(), 'end': match.end(),
                'labels': sorted(owners),
                'reason': ('Соответствует нескольким сотрудникам' if len(owners) > 1
                           else 'После замены остались известные данные'),
            })
    for kind, expression in (
        ('unknown_email', r'(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+'),
        ('unknown_phone', r'(?<![\w+])\+?\d(?:[\s().-]*\d){6,14}(?!\w)'),
    ):
        for match in re.finditer(expression, text):
            if kind == 'unknown_phone' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', match[0]):
                continue
            if any(match.start() < item['end'] and match.end() > item['start']
                   for item in findings):
                continue
            findings.append({'kind': kind, 'value': match[0],
                             'start': match.start(), 'end': match.end(), 'labels': [],
                             'reason': 'Контакт не удалось сопоставить со справочником'})
    findings.sort(key=lambda item: item['start'])
    for label in reserved:
        marker = 'LITERAL_' + label
        start = text.find(marker)
        findings.append({'kind': 'reserved_label', 'value': label,
                         'start': start, 'end': start + len(marker), 'labels': [],
                         'reason': 'В исходном тексте уже была служебная метка; '
                                   'она сохранена как LITERAL_' + label})
    return text, {label: names[label] for label in sorted(used)}, findings


def restore_text(text, names):
    pattern = r'(?<!\w)EMP_[0-9]+(?!\w)'
    unknown = sorted(set(re.findall(pattern, text)) - set(names))
    if unknown:
        raise ValueError('В ответе неизвестные метки: ' + ', '.join(unknown))
    return re.sub(pattern, lambda match: names[match[0]], text)
