# LLD: Resilient Form Scraper & Applier

## 1. Goals & Non-Goals

### Goals
- Extractor captures custom web components, Shadow DOM elements, and ARIA-interactive controls that the current `input, select, textarea` query misses
- Selector strategy is stable across ATS DOM restructuring (data-automation-id → ARIA label → name/id priority chain)
- Executor never hard-crashes on unknown field types; falls back through a strategy ladder
- Every fill failure auto-saves a reproduction bundle (HTML context + screenshot + metadata) for developer triage
- Items 1 and 2 are self-contained; item 3 (strategy learning + control zoo) is a future extension that consumes artifacts produced here

### Non-Goals
- LLM-powered field classification (deferred to future)
- Vision agent fallback (deferred to future)
- Distributed infra changes
- Modifying stage or gate business logic

---

## 2. Dependency Chain

Items 1 and 2 are directly coupled. Item 1 can produce `field_type = "unknown"` for unrecognized ARIA roles; item 2 is the handler for that type.

```
Item 1: Enhanced Extraction
    │  produces: richer ExtractedField (aria_role, extraction_source, priority selector chain)
    │  produces: field_type = "unknown" for unrecognized ARIA roles
    ▼
Item 2: Fallback Fill Chain + Auto-Capture
    │  handles: "unknown" field_type via strategy ladder
    │  produces: strategy_used on FieldOutcome
    │  produces: failure bundles at logs/{run_id}/failures/{field_id}/
    ▼
Item 3 (future): K (strategy learning) + L (control zoo)
    consumes: failure bundles + strategy_used outcomes
```

---

## 3. Item 1 — Enhanced Extraction

### 3.1 Model Changes (`models.py`)

Two new fields on `ExtractedField`, backward compatible with all existing callers:

```python
class ExtractedField(BaseModel):
    # ... all existing fields unchanged ...
    aria_role: str = ""
    # raw role attribute from DOM; empty string when absent
    extraction_source: Literal["dom", "shadow_dom"] = "dom"
    # "shadow_dom" when the element was found inside a shadow root
```

New literal added to `FieldType`:

```python
FieldType = Literal[
    "text", "textarea", "select", "radio", "checkbox",
    "combobox_search", "combobox_lazy", "date", "file", "hidden",
    "unknown",   # unrecognized ARIA role; triggers fallback chain in executor
]
```

Two new fields on `FieldOutcome` (consumed by item 3 later; unused until then):

```python
class FieldOutcome(BaseModel):
    # ... all existing fields unchanged ...
    strategy_used: str | None = None
    # "typed" | "generic_fill" | "generic_type" | "js_value_inject" | "contenteditable_fill"
    failure_bundle_path: str | None = None
    # absolute path to failure bundle dir when status="fill_error"; None otherwise
```

### 3.2 Enhanced JS Extractor (`extractor.py`)

Replace the current `root.locator("input, select, textarea").evaluate_all(...)` call with a new JS
function that:

1. Queries an extended selector set (standard inputs + ARIA-interactive elements)
2. Recursively traverses shadow roots (max depth 3 to avoid infinite loops)
3. Captures new metadata: `role`, `data_automation_id`, `data_testid`, `contenteditable`
4. Returns the same shape as today plus the new fields

**JS to inject:**

```javascript
const INTERACTIVE = [
  'input:not([type="hidden"])',
  'select',
  'textarea',
  '[role="combobox"]',
  '[role="listbox"]',
  '[role="radio"]',
  '[role="checkbox"]',
  '[role="spinbutton"]',
  '[role="searchbox"]',
  '[role="textbox"]',
  '[role="switch"]',
  '[contenteditable="true"]',
].join(', ');

function collectElements(root, fromShadow) {
  const results = [];
  root.querySelectorAll(INTERACTIVE).forEach(el => {
    results.push({ el, fromShadow });
  });
  root.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) {
      collectElements(el.shadowRoot, true).forEach(r => results.push(r));
    }
  });
  return results;
}

const rootEl = arguments[0];  // injected by Playwright
return collectElements(rootEl, false).map(({ el, fromShadow }, i) => {
  const tag  = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const role = el.getAttribute('role') || '';
  const name = el.getAttribute('name') || '';
  const id   = el.id || '';
  const dataAutomationId = el.getAttribute('data-automation-id') || '';
  const dataTestId       = el.getAttribute('data-testid') || '';
  const contentEditable  = el.getAttribute('contenteditable') === 'true';

  const label =
    el.getAttribute('aria-label') ||
    (el.labels?.[0]?.textContent?.trim()) ||
    el.getAttribute('placeholder') ||
    dataAutomationId ||
    name || id || `field_${i}`;

  const required = el.required || el.getAttribute('aria-required') === 'true';

  let options = [];
  if (tag === 'select') {
    options = Array.from(el.options || []).map(o => o.textContent.trim()).filter(Boolean);
  }

  let prefilled = '';
  if (tag === 'select') {
    prefilled = el.selectedOptions?.[0]?.textContent?.trim() || '';
  } else if (type === 'checkbox' || type === 'radio') {
    prefilled = el.checked ? (el.value || 'true') : '';
  } else {
    prefilled = (el.value || el.textContent || '').trim();
  }

  return {
    tag, type, role, name, id,
    dataAutomationId, dataTestId, contentEditable,
    label, required, options, prefilled, fromShadow, idx: i,
  };
});
```

**Deduplication**: Shadow DOM traversal can return the same element twice (once from the outer
query, once from walking shadow roots). After collecting raw results, deduplicate by the tuple
`(name or "", id or "", dataAutomationId or "")` before building `ExtractedField` objects.
Shadow-origin wins over DOM-origin for the same element (more specific source).

### 3.3 Expanded Field Type Classifier (`extractor.py`)

Replace `_classify_field_type(tag, input_type)` with `_classify_field_type(tag, input_type, aria_role, contenteditable)`:

```python
_ARIA_ROLE_MAP: dict[str, FieldType] = {
    "combobox":   "combobox_search",
    "listbox":    "select",
    "radio":      "radio",
    "checkbox":   "checkbox",
    "switch":     "checkbox",
    "spinbutton": "text",
    "searchbox":  "text",
    "textbox":    "text",
}

def _classify_field_type(
    tag: str,
    input_type: str,
    aria_role: str = "",
    contenteditable: bool = False,
) -> FieldType:
    # ARIA role takes precedence — handles custom web components
    if aria_role:
        if aria_role in _ARIA_ROLE_MAP:
            return _ARIA_ROLE_MAP[aria_role]
        return "unknown"   # known ARIA element, strategy unknown → fallback chain

    # contenteditable div = rich textarea (cover letter editors, etc.)
    if contenteditable:
        return "textarea"

    # Existing logic — unchanged
    if tag == "textarea":        return "textarea"
    if tag == "select":          return "select"
    if input_type == "radio":    return "radio"
    if input_type == "checkbox": return "checkbox"
    if input_type == "date":     return "date"
    if input_type == "file":     return "file"
    if input_type == "hidden":   return "hidden"
    return "text"
```

### 3.4 Selector Priority Chain (`extractor.py`)

Replace `selector = f"[name={field_id_hint!r}], [id={field_id_hint!r}]"` with `_build_selector()`:

```python
def _build_selector(
    name: str,
    id_: str,
    label: str,
    data_automation_id: str,
    data_testid: str,
) -> str:
    parts: list[str] = []

    # Platform-specific data attributes (most stable — ATS contract, not implementation detail)
    if data_automation_id:
        parts.append(f'[data-automation-id="{data_automation_id}"]')
    if data_testid:
        parts.append(f'[data-testid="{data_testid}"]')

    # ARIA label (survives React re-renders that change class/id)
    if label and not label.startswith("field_"):
        escaped = label.replace('"', '\\"')
        parts.append(f'[aria-label="{escaped}"]')

    # Standard HTML attributes (fallback)
    if name:
        parts.append(f'[name="{name}"]')
    if id_:
        parts.append(f'[id="{id_}"]')

    return ", ".join(parts) if parts else "body"
```

> **Why this order**: `data-automation-id` is Workday's published stable contract. ARIA labels
> survive React re-renders. `name`/`id` are last because SPAs regularly regenerate them.

---

## 4. Item 2 — Fallback Fill Chain + Auto-Capture

### 4.1 Strategy Ladder (`executor.py`)

Extract `_fill_field` into a pipeline of independent strategies. Each strategy is an async callable:

```
FillStrategy = Callable[[Page, ExtractedField, str], Awaitable[None]]
# Raises on failure. Returns None on success.
```

**Strategies in priority order:**

| # | Name | Mechanism | Works when |
|---|---|---|---|
| 1 | `typed` | Existing `match field.field_type` block (unchanged) | Known field types with native HTML controls |
| 2 | `generic_fill` | `loc.fill(value)` | Text-like inputs accepting programmatic fill |
| 3 | `generic_type` | `loc.click(); loc.type(value, delay=30)` | Inputs needing simulated keystrokes |
| 4 | `js_value_inject` | `page.evaluate(...)` sets `.value` + dispatches `input`+`change` events with `bubbles:true` | React/Angular controlled inputs that ignore Playwright fill |
| 5 | `contenteditable_fill` | `loc.click()` → `Ctrl+A` → `keyboard.type(value)` | Rich text editors, `contenteditable` divs |

**Execution function:**

```python
async def _fill_field_with_fallback(
    page: object,
    field: ExtractedField,
    value: str,
) -> tuple[str, list[str]]:
    """
    Tries each strategy in order.
    Returns (strategy_name_that_succeeded, [per_strategy_error_strings]).
    Raises FillError if all strategies are exhausted.
    """
    strategies: list[tuple[str, FillStrategy]] = [
        ("typed",                _strategy_typed),
        ("generic_fill",         _strategy_generic_fill),
        ("generic_type",         _strategy_generic_type),
        ("js_value_inject",      _strategy_js_inject),
        ("contenteditable_fill", _strategy_contenteditable),
    ]

    errors: list[str] = []
    for name, strategy in strategies:
        try:
            await strategy(page, field, value)
            return name, errors
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    raise FillError(
        f"All strategies exhausted for field '{field.label}':\n" + "\n".join(errors)
    )
```

**`_strategy_typed`** is the current `_fill_field` body (the `match field.field_type` block)
extracted verbatim. No logic changes.

**`_strategy_js_inject`** implementation:

```python
async def _strategy_js_inject(page: object, field: ExtractedField, value: str) -> None:
    loc = page.locator(field.selector).first
    await loc.wait_for(state="visible", timeout=5_000)
    await page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel.split(',')[0].trim());
            if (!el) throw new Error('element not found');
            el.value = val;
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [field.selector, value],
    )
```

### 4.2 Auto-Capture Bundle (`executor.py`)

A single function, called from `execute_page` when `FillError` is raised:

```python
async def _capture_failure_bundle(
    page: object,
    field: ExtractedField,
    instruction: FillInstruction,
    errors: list[str],
    run_id: str,
) -> str:
    """Saves reproduction artifacts. Returns bundle directory path."""
    from pathlib import Path
    import json

    bundle_dir = Path("logs") / run_id / "failures" / field.id
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # 1. Field + instruction metadata
    (bundle_dir / "field.json").write_text(
        json.dumps({
            "field":       field.model_dump(mode="json"),
            "instruction": instruction.model_dump(mode="json"),
            "errors":      errors,
        }, indent=2),
        encoding="utf-8",
    )

    # 2. HTML context: element + 2 levels of parent DOM
    try:
        ctx_html = await page.evaluate(
            """sel => {
                const el = document.querySelector(sel.split(',')[0].trim());
                if (!el) return '<element not found in DOM>';
                let node = el;
                for (let i = 0; i < 2 && node.parentElement; i++) node = node.parentElement;
                return node.outerHTML;
            }""",
            field.selector,
        )
        (bundle_dir / "context.html").write_text(ctx_html, encoding="utf-8")
    except Exception:
        pass  # best-effort; don't let capture failure mask the original error

    # 3. Full-page screenshot
    try:
        await page.screenshot(path=str(bundle_dir / "screenshot.png"), full_page=True)
    except Exception:
        pass

    return str(bundle_dir)
```

**Integration in `FormExecutor.execute_page`** — replace the existing `except Exception as exc` block:

```python
except Exception as exc:
    errors = getattr(exc, "strategy_errors", [str(exc)])
    bundle_path: str | None = None
    if inst:
        bundle_path = await _capture_failure_bundle(page, field, inst, errors, run_id)

    outcomes.append(FieldOutcome(
        field_id=field.id,
        action_taken="fill",
        value_used=inst.value if inst else None,
        status="fill_error",
        error_message=str(exc),
        strategy_used=None,
        failure_bundle_path=bundle_path,
    ))
```

On success, set `strategy_used`:

```python
strategy_name, _ = await _fill_field_with_fallback(page, field, inst.value)
outcomes.append(FieldOutcome(
    field_id=field.id,
    action_taken="fill",
    value_used=inst.value,
    status="ok",
    error_message=None,
    strategy_used=strategy_name,
    failure_bundle_path=None,
))
```

### 4.3 Required-Field Submission Gate

The executor no longer hard-crashes, so the decision to abort submission moves up one level to
`FormSubmissionStage`. Add a post-execution check before calling `adapter.advance()`:

```python
required_failures = [
    o for o in outcomes
    if o.status == "fill_error"
    and field_map[o.field_id].required
]
if required_failures:
    raise RequiredFieldFillError(
        f"{len(required_failures)} required field(s) could not be filled — aborting submission",
        failed_field_ids=[o.field_id for o in required_failures],
    )
```

`RequiredFieldFillError` is a new exception in `exceptions.py`; it carries `failed_field_ids` so
the caller can log which fields blocked submission.

`execute_page` needs `run_id` threaded through from the caller. Add it as a parameter:
`async def execute_page(self, page, fields, instructions, run_id: str)`.

---

## 5. Item 3 (Future) — K: Strategy Learning + L: Control Zoo

*Design is fixed. Implementation is deferred. Do not implement during item 1/2 work.*

### 5.1 K: Strategy Learning Store

**New file**: `src/autorole/integrations/form_controls/strategy_store.py`

**New migration**: `src/autorole/db/migrations/003_strategy_store.sql`

```sql
CREATE TABLE IF NOT EXISTS fill_strategy_outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dom_signature TEXT    NOT NULL,
    platform_id   TEXT    NOT NULL,
    strategy      TEXT    NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    last_seen     TEXT,
    UNIQUE(dom_signature, platform_id, strategy)
);
```

**DOM signature**: `sha256(f"{aria_role}|{tag}|{bool(name)}|{bool(data_automation_id)}|{normalized_label}")[:16]`

Stable across runs, not tied to specific values. Normalized label = lowercase, strip punctuation.

**Pre-fill query**: Before the strategy ladder, ask the store for the historically best strategy
for this `(dom_signature, platform_id)` pair. If found, put it first in the ladder.

**Post-fill record**: After every outcome (success or failure), call
`strategy_store.record(dom_signature, platform_id, strategy_used, success)`.

**Canonical seeding from L**: `StrategyStore.seed_from_catalog(catalog_path)` reads L's
`catalog.json` and inserts rows with `success_count=999` (canonical entries always rank first).
Called once at process startup.

### 5.2 L: Control Zoo

**New directory**: `tests/zoo/`

```
tests/zoo/
├── catalog.json        # dom_signature → canonical_strategy for each known control
├── controls.html       # static HTML: one representative element per known control type
├── conftest.py         # pytest fixture: serves controls.html on localhost
└── test_zoo.py         # one test per entry in catalog.json
```

**`catalog.json` entry shape:**

```json
{
  "id": "greenhouse_text",
  "description": "Standard Greenhouse plain-text input",
  "dom_signature": "abc123def456",
  "platform_id": "greenhouse",
  "canonical_strategy": "typed",
  "html_element_id": "gh-text-input"
}
```

**K → L promotion flow** (human-in-the-loop, manual commit):

```
logs/{run_id}/failures/{field_id}/        ← auto-captured by item 2
    │
    ▼  dev reviews context.html + screenshot
    │  identifies new control type
    │
    ▼  copies control HTML into tests/zoo/controls.html
    │  adds entry to catalog.json with canonical_strategy
    │  writes test in test_zoo.py
    │  runs: pytest tests/zoo/
    │
    ▼  if green → merges; K's table seeded via seed_from_catalog()
```

**Invariant**: L never auto-reads from K. K reads L only at startup. Promotion is always a
reviewed commit. This prevents K from cementing accidental successes as canonical strategies.

---

## 6. File Change Summary

| File | Item | Change type |
|---|---|---|
| `src/autorole/integrations/form_controls/models.py` | 1 + 2 | Add `aria_role`, `extraction_source` to `ExtractedField`; add `strategy_used`, `failure_bundle_path` to `FieldOutcome`; add `"unknown"` to `FieldType` |
| `src/autorole/integrations/form_controls/extractor.py` | 1 | Replace JS query, `_classify_field_type`, add `_build_selector` |
| `src/autorole/integrations/form_controls/executor.py` | 2 | Replace `_fill_field` with strategy ladder; add `_capture_failure_bundle`; thread `run_id` through `execute_page`; update success/failure outcome construction |
| `src/autorole/integrations/form_controls/exceptions.py` | 2 | Add `RequiredFieldFillError` |
| `src/autorole/stages/form_submission.py` | 2 | Add required-field gate before `adapter.advance()` |
| `src/autorole/integrations/form_controls/strategy_store.py` | 3 future | New file |
| `src/autorole/db/migrations/003_strategy_store.sql` | 3 future | New migration |
| `tests/zoo/` | 3 future | New directory |
