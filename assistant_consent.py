"""Explicit consent language shared by intake and approval handling."""
import re

LEGACY = re.compile(r'^(确认|拒绝)协作\s+([A-Fa-f0-9]{8})$')
NATURAL = re.compile(r'^(允许|同意|确认|拒绝|取消)本次协作[。！!]?$', re.UNICODE)


def parse(text):
    text = (text or '').strip()
    match = LEGACY.fullmatch(text)
    if match:
        return ('deny' if match[1] == '拒绝' else 'allow', match[2].upper())
    match = NATURAL.fullmatch(text)
    if match:
        return ('deny' if match[1] in ('拒绝', '取消') else 'allow', None)
    return None
