# Implementation Plan

## Overview

Three changes to the Telegram bot: fix mobile layout overflow in leads view, add paid-lawyer disclaimers across all relevant user touchpoints, and remove hardcoded example messages from greetings.

---

## Task 1: Leads View — Remove `🔍` and `#` from Buttons (Mobile Overflow Fix)

### Problem
The `_leads_list_keyboard()` function generates inline keyboard buttons with text like `🔍 #1 Ochish` or `🔍 #3`. On mobile Telegram clients these emoji + hash + number combos cause text wrapping/overflow.

### Affected File

#### [`message_handlers.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/infrastructure/telegram/handlers/message_handlers.py#L548-L628)

### Exact Change (Lines 564–565)

**Before:**
```python
btn_text = f"🔍 #{i} Ochish" if leads_count <= 4 else f"🔍 #{i}"
```

**After:**
```python
btn_text = f"{i} Ochish" if leads_count <= 4 else f"{i}"
```

### Checklist
- [ ] Update `btn_text` in `_leads_list_keyboard()` (line 565) — remove `🔍 #` prefix from both variants.
- [ ] Verify no other code references `🔍 #` for parsing or display.

---

## Task 2: Paid Lawyer Disclaimer

### Problem
Users are not informed that lawyer services are paid. A disclaimer must be added to:
1. All initial greeting / welcome messages.
2. The flow where a user requests or is matched with a lawyer.
3. Booking / handoff / confirmation (escalation) steps.

### Proposed Disclaimer Copy (Uzbek)

> **Primary (for greetings and welcome screens):**
>
> `💰 Advokat xizmatlari pullik asosda ko'rsatiladi.`

> **Extended (for escalation / handoff confirmations):**
>
> `💰 Eslatma: Advokat Jasurbek jamoasining konsultatsiya va ish yuritish xizmatlari pullik.`

### Affected Files & Exact Locations

---

#### A. [`message_handlers.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/infrastructure/telegram/handlers/message_handlers.py) — Welcome/Greeting Messages

| # | Function | Lines | What to add |
|---|----------|-------|-------------|
| 1 | `_greeting_message()` | 291–310 | Append `\n💰 <i>Advokat xizmatlari pullik asosda ko'rsatiladi.</i>` before the closing `👇` line. |
| 2 | `_subscription_required_message()` | 313–327 | Append `\n💰 <i>Advokat xizmatlari pullik asosda ko'rsatiladi.</i>` before the closing `👇` line. |
| 3 | `_subscription_confirmed_message()` | 330–341 | Append `\n💰 <i>Advokat xizmatlari pullik asosda ko'rsatiladi.</i>` before the closing `👇` line. |
| 4 | `cmd_help()` | 1789–1816 | Add `\n💰 <i>Advokat xizmatlari pullik asosda ko'rsatiladi.</i>\n` in the help text, near the end before the closing line. |

---

#### B. [`message_handlers.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/infrastructure/telegram/handlers/message_handlers.py) — Escalation Confirmation to User (Lawyer Request Flow)

| # | Location | Lines | What to add |
|---|----------|-------|-------------|
| 5 | `on_user_message` — escalated state reply | 179–183 | Add `💰 <i>Eslatma: konsultatsiya va ish yuritish xizmatlari pullik.</i>` to the reply text. |

---

#### C. [`handle_user_message.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/application/use_cases/conversation/handle_user_message.py) — Escalation Confirmation

| # | Location | Lines | What to add |
|---|----------|-------|-------------|
| 6 | `execute()` — escalation_msg | 418–422 | Add `\n\n💰 <i>Eslatma: Advokat Jasurbek jamoasining konsultatsiya va ish yuritish xizmatlari pullik.</i>` to the escalation confirmation message. |
| 7 | `_AI_FAILURE_FALLBACK` | 48–51 | Add `Advokat xizmatlari pullik asosda ko'rsatiladi.` to the fallback message. |

---

#### D. [`context_aware_response.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/application/context/context_aware_response.py) — Rule-Based Responses

| # | Location | Lines | What to add |
|---|----------|-------|-------------|
| 8 | `GREETING_RESPONSE` | 70–79 | Add `\n💰 <i>Advokat xizmatlari pullik asosda ko'rsatiladi.</i>` before the closing line. |
| 9 | `SERVICE_REQUEST_RESPONSE` | 81–86 | Add `\n💰 <i>Eslatma: advokat xizmatlari pullik asosda ko'rsatiladi.</i>` to this response. |
| 10 | Contextual greeting (has_problem variant) | 132–138 | Add disclaimer line. |
| 11 | Contextual service request (has_problem variant) | 111–117 | Add disclaimer line. |

---

#### E. [`gemini_chat_adapter.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/infrastructure/ai/gemini_chat_adapter.py) — System Prompt

| # | Location | Lines | What to add |
|---|----------|-------|-------------|
| 12 | `DEFAULT_SYSTEM_PROMPT` — JASURBEK HAQIDA section | 99–100 | Already states `Konsultatsiya va ish yuritish — pullik.` ✅ No change needed. |
| 13 | `DEFAULT_SYSTEM_PROMPT` — ESKALATSIYA QARORLARI section | 136–139 | Add instruction: `Foydalanuvchiga advokat yo'naltirilganda, xizmatlar pullik ekanligini eslatib qo'y.` |

### Checklist
- [ ] `_greeting_message()` — add disclaimer
- [ ] `_subscription_required_message()` — add disclaimer
- [ ] `_subscription_confirmed_message()` — add disclaimer
- [ ] `cmd_help()` — add disclaimer
- [ ] Escalated state reply in `on_user_message` (line 179–183) — add disclaimer
- [ ] `escalation_msg` in `handle_user_message.py` (line 418–422) — add disclaimer
- [ ] `_AI_FAILURE_FALLBACK` — add disclaimer
- [ ] `GREETING_RESPONSE` in `context_aware_response.py` — add disclaimer
- [ ] `SERVICE_REQUEST_RESPONSE` in `context_aware_response.py` — add disclaimer
- [ ] Contextual greeting variant — add disclaimer
- [ ] Contextual service request variant — add disclaimer
- [ ] System prompt `ESKALATSIYA QARORLARI` section — add instruction to mention paid services

---

## Task 3: Remove Hardcoded Example Messages from Greeting

### Problem
The `_greeting_message()` function (lines 291–310) contains four hardcoded example messages (`▫️ "Ish beruvchim..."`, `▫️ "2 nafar farzandim..."`, etc.) that clutter the greeting screen. These should be removed.

Similarly, `_subscription_confirmed_message()` (lines 330–341) contains one hardcoded example.

### Affected File

#### [`message_handlers.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/infrastructure/telegram/handlers/message_handlers.py#L291-L341)

### Exact Changes

**`_greeting_message()` — Remove lines 303–307** (the `Masalan:` header + four `▫️` example blocks):
```python
# REMOVE these lines:
        "<b>Masalan:</b>\n"
        "▫️ <i>"Ish beruvchim meni ogohlantirmasdan ishdan bo'shatdi. Nima qilishim mumkin?"</i>\n\n"
        "▫️ <i>"2 nafar farzandim uchun aliment qancha bo'ladi?"</i>\n\n"
        "▫️ <i>"Menga shartnoma bo'yicha da'vo kelgan, nima qilishim kerak?"</i>\n\n"
        "▫️ <i>"Merosni qanday rasmiylashtirish mumkin?"</i>\n\n"
```

**`_subscription_confirmed_message()` — Remove lines 338–339** (the example):
```python
# REMOVE these lines:
        "<b>Masalan:</b>\n"
        "<i>"Ish beruvchim meni 3 kun oldin ishdan bo'shatdi. Hech qanday ogohlantirish berilmagan. Menda mehnat shartnomasi bor."</i>\n\n"
```

### Checklist
- [ ] Remove `<b>Masalan:</b>` + four `▫️` example lines from `_greeting_message()`
- [ ] Remove `<b>Masalan:</b>` + example from `_subscription_confirmed_message()`

---

## Summary of All Files to Modify

| File | Changes |
|------|---------|
| [`message_handlers.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/infrastructure/telegram/handlers/message_handlers.py) | Tasks 1, 2 (A/B), 3 |
| [`handle_user_message.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/application/use_cases/conversation/handle_user_message.py) | Task 2 (C) |
| [`context_aware_response.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/application/context/context_aware_response.py) | Task 2 (D) |
| [`gemini_chat_adapter.py`](file:///media/bexzodx/Новый%20том/AI-ML/projects/lawyer-bot-production/src/infrastructure/ai/gemini_chat_adapter.py) | Task 2 (E) |

---

## Verification Plan

- [ ] Run existing tests: `python -m pytest tests/`
- [ ] Grep for any remaining `🔍 #` or `🔍` patterns in button text to confirm full removal.
- [ ] Grep for `Masalan:` to confirm example removal is complete.
- [ ] Grep for `pullik` to verify disclaimer is present in all required locations.
- [ ] Manual QA: `/start`, `/help`, send a greeting, trigger escalation flow, view `/leads` in admin group — verify disclaimer presence and no mobile overflow.
