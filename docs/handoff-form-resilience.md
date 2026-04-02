# Handoff: Resilient Form Scraper & Applier — Items 1 & 2

## Context

AutoRole automates job applications. The form scraping and filling pipeline is the most brittle
part: when an ATS uses custom web components, ARIA-interactive controls, or Shadow DOM, the
extractor misses fields and the executor hard-crashes on unknown types. This task makes both
layers resilient without changing any business logic.

Read the full LLD before writing any code: `docs/lld-form-resilience.md`

Before writing any code, read these files in full to understand what already exists:

- `src/autorole/integrations/form_controls/extractor.py` — `SemanticFieldExtractor`, `_classify_field_type` (item 1 changes here)
- `src/autorole/integrations/form_controls/executor.py` — `FormExecutor`, `_fill_field` (item 2 changes here)
- `src/autorole/integrations/form_controls/models.py` — all data models (item 1 + 2 changes here)
- `src/autorole/integrations/form_controls/exceptions.py` — exception hierarchy (item 2 adds here)
- `src/autorole/stages/form_submission.py` — `FormSubmissionStage.execute()` — understand the existing `failed_outcomes` check at line 84–108 before touching it
- `tests/integration/test_form_submission_worker.py` — understand the test pattern
- `tests/conftest.py` — `load_fixture`, `make_worker_message`, `MockStage`

---

## Absolute Constraints

- **Do not modify** any file under `src/autorole/stages/form_intelligence.py` or
  `src/autorole/context.py` or any gate/queue logic.
- **Do not change** the public signature of `FormExecutor.execute_page` beyond adding `run_id: str`
  as a parameter — all existing callers must still work.
- **Do not change** any existing `FieldType` literal values — only add `"unknown"`.
- **Do not change** `_build_audit_log` or `_write_audit_log` — they are not part of this task.
- The `selector` field on `ExtractedField` must remain a comma-separated CSS selector string —
  the executor uses `page.locator(field.selector).first` directly.
- All new code must be `async`-first with `from __future__ import annotations` at the top.
- Type-annotate everything. No `Any` except where existing code already uses it.
- Do **not** implement item 3 (strategy store, control zoo) — design only, deferred. Do not create
  `strategy_store.py`, `003_strategy_store.sql`, or `tests/zoo/`.

---

## Implementation Order

Follow this order strictly. Each step builds on the previous.

---

### Step 1 — Model changes (`models.py`)

**What to add:**

1. `"unknown"` to the `FieldType` literal — placed last in the list.

2. Two new optional fields on `ExtractedField` with defaults so all existing construction sites
   continue to work without changes:

   ```python
   aria_role: str = ""
   extraction_source: Literal["dom", "shadow_dom"] = "dom"
   ```

3. Two new optional fields on `FieldOutcome` with defaults:

   ```python
   strategy_used: str | None = None
   failure_bundle_path: str | None = None
   ```

**Verification**: Run the existing test suite after this step only — it must pass with zero
changes to any test file. The new fields all have defaults, so no existing construction site
breaks.

---

### Step 2 — Add `RequiredFieldFillError` to `exceptions.py`

```python
class RequiredFieldFillError(JobAutomationError):
    def __init__(self, msg: str, failed_field_ids: list[str] | None = None) -> None:
        super().__init__(msg)
        self.failed_field_ids: list[str] = failed_field_ids or []
```

---

### Step 3 — Enhanced extractor (`extractor.py`)

Three changes in this file. Do them in this order.

**3a. Add `_ARIA_ROLE_MAP` and update `_classify_field_type`**

Replace the existing `_classify_field_type(tag, input_type)` function with:

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
    if aria_role:
        if aria_role in _ARIA_ROLE_MAP:
            return _ARIA_ROLE_MAP[aria_role]
        return "unknown"
    if contenteditable:
        return "textarea"
    if tag == "textarea":        return "textarea"
    if tag == "select":          return "select"
    if input_type == "radio":    return "radio"
    if input_type == "checkbox": return "checkbox"
    if input_type == "date":     return "date"
    if input_type == "file":     return "file"
    if input_type == "hidden":   return "hidden"
    return "text"
```

**3b. Add `_build_selector`**

New function, placed after `_classify_field_type`:

```python
def _build_selector(
    name: str,
    id_: str,
    label: str,
    data_automation_id: str,
    data_testid: str,
) -> str:
    parts: list[str] = []
    if data_automation_id:
        parts.append(f'[data-automation-id="{data_automation_id}"]')
    if data_testid:
        parts.append(f'[data-testid="{data_testid}"]')
    if label and not label.startswith("field_"):
        escaped = label.replace('"', '\\"')
        parts.append(f'[aria-label="{escaped}"]')
    if name:
        parts.append(f'[name="{name}"]')
    if id_:
        parts.append(f'[id="{id_}"]')
    return ", ".join(parts) if parts else "body"
```

**3c. Replace the JS query and field-building loop in `SemanticFieldExtractor.extract`**

Replace the entire `raw_fields = await root.locator(...).evaluate_all(...)` call and the
`for item in raw_fields` loop below it.

New JS to inject (pass `root` element as the argument):

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
  root.querySelectorAll(INTERACTIVE).forEach(el => results.push({ el, fromShadow }));
  root.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) collectElements(el.shadowRoot, true).forEach(r => results.push(r));
  });
  return results;
}

return collectElements(arguments[0], false).map(({ el, fromShadow }, i) => {
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
  if (tag === 'select') prefilled = el.selectedOptions?.[0]?.textContent?.trim() || '';
  else if (type === 'checkbox' || type === 'radio') prefilled = el.checked ? (el.value || 'true') : '';
  else prefilled = (el.value || el.textContent || '').trim();
  return { tag, type, role, name, id, dataAutomationId, dataTestId, contentEditable,
           label, required, options, prefilled, fromShadow, idx: i };
});
```

New field-building loop:

```python
# Deduplicate: shadow-origin wins over dom-origin for the same element
seen: set[tuple[str, str, str]] = set()
fields: list[ExtractedField] = []

for item in raw_fields:
    name              = str(item.get("name") or "").strip()
    id_               = str(item.get("id") or "").strip()
    data_automation_id = str(item.get("dataAutomationId") or "").strip()
    data_testid       = str(item.get("dataTestId") or "").strip()
    tag               = str(item.get("tag") or "input").lower()
    input_type        = str(item.get("type") or "").lower()
    aria_role         = str(item.get("role") or "").lower()
    contenteditable   = bool(item.get("contentEditable", False))
    from_shadow       = bool(item.get("fromShadow", False))

    dedup_key = (name, id_, data_automation_id)
    if dedup_key != ("", "", "") and dedup_key in seen:
        continue
    seen.add(dedup_key)

    field_id_hint = name or id_ or data_automation_id or f"field_{item.get('idx', len(fields))}"
    field_type    = _classify_field_type(tag, input_type, aria_role, contenteditable)
    selector      = _build_selector(name, id_, str(item.get("label") or ""), data_automation_id, data_testid)
    stable_id     = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{page_index}:{field_id_hint}:{selector}"))

    fields.append(
        ExtractedField(
            id=stable_id,
            run_id=run_id,
            page_index=page_index,
            page_label=page_label,
            field_type=field_type,
            selector=selector,
            label=str(item.get("label") or field_id_hint),
            required=bool(item.get("required", False)),
            options=[str(opt) for opt in item.get("options", [])],
            prefilled_value=str(item.get("prefilled") or ""),
            aria_role=aria_role,
            extraction_source="shadow_dom" if from_shadow else "dom",
        )
    )
```

Note: `evaluate_all` cannot pass a root element directly. Replace the `root.locator(...).evaluate_all(...)` pattern with `root.evaluate_handle` to get the DOM node, then use `page.evaluate(js, element_handle)`. See Playwright docs for `evaluate` with a handle argument.

**Verification**: Unit-test `_classify_field_type` and `_build_selector` directly. The full
integration tests for `FormIntelligenceWorker` must still pass.

---

### Step 4 — Strategy ladder + auto-capture (`executor.py`)

Four changes in this file.

**4a. Add `_capture_failure_bundle`**

New module-level async function (add after the existing `_fill_field`):

```python
async def _capture_failure_bundle(
    page: object,
    field: ExtractedField,
    instruction: FillInstruction,
    errors: list[str],
    run_id: str,
) -> str:
    import json as _json
    bundle_dir = Path("logs") / run_id / "failures" / field.id
    bundle_dir.mkdir(parents=True, exist_ok=True)

    (bundle_dir / "field.json").write_text(
        _json.dumps({
            "field":       field.model_dump(mode="json"),
            "instruction": instruction.model_dump(mode="json"),
            "errors":      errors,
        }, indent=2),
        encoding="utf-8",
    )

    try:
        ctx_html = await page.evaluate(
            """sel => {
                const el = document.querySelector(sel.split(',')[0].trim());
                if (!el) return '<element not found>';
                let node = el;
                for (let i = 0; i < 2 && node.parentElement; i++) node = node.parentElement;
                return node.outerHTML;
            }""",
            field.selector,
        )
        (bundle_dir / "context.html").write_text(ctx_html, encoding="utf-8")
    except Exception:
        pass

    try:
        await page.screenshot(path=str(bundle_dir / "screenshot.png"), full_page=True)
    except Exception:
        pass

    return str(bundle_dir)
```

**4b. Rename existing `_fill_field` → `_strategy_typed`**

No logic changes. Just rename. This is strategy #1 in the ladder.

**4c. Add remaining strategy functions**

```python
async def _strategy_generic_fill(page: object, field: ExtractedField, value: str) -> None:
    loc = page.locator(field.selector).first
    await loc.wait_for(state="visible", timeout=5_000)
    await loc.fill(value)


async def _strategy_generic_type(page: object, field: ExtractedField, value: str) -> None:
    loc = page.locator(field.selector).first
    await loc.wait_for(state="visible", timeout=5_000)
    await loc.click()
    await loc.type(value, delay=30)


async def _strategy_js_inject(page: object, field: ExtractedField, value: str) -> None:
    loc = page.locator(field.selector).first
    await loc.wait_for(state="visible", timeout=5_000)
    await page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel.split(',')[0].trim());
            if (!el) throw new Error('element not found: ' + sel);
            el.value = val;
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [field.selector, value],
    )


async def _strategy_contenteditable(page: object, field: ExtractedField, value: str) -> None:
    loc = page.locator(field.selector).first
    await loc.wait_for(state="visible", timeout=5_000)
    await loc.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.type(value)
```

**4d. Add `_fill_field_with_fallback` and wire into `execute_page`**

```python
_FILL_STRATEGIES: list[tuple[str, object]] = [
    ("typed",                _strategy_typed),
    ("generic_fill",         _strategy_generic_fill),
    ("generic_type",         _strategy_generic_type),
    ("js_value_inject",      _strategy_js_inject),
    ("contenteditable_fill", _strategy_contenteditable),
]


async def _fill_field_with_fallback(
    page: object, field: ExtractedField, value: str
) -> tuple[str, list[str]]:
    """Returns (strategy_used, per_strategy_errors). Raises FillError if all exhausted."""
    errors: list[str] = []
    for name, strategy in _FILL_STRATEGIES:
        try:
            await strategy(page, field, value)
            return name, errors
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise FillError(
        f"All strategies exhausted for field '{field.label}':\n" + "\n".join(errors)
    )
```

Update `FormExecutor.execute_page` signature to accept `run_id`:

```python
async def execute_page(
    self,
    page: object,
    fields: list[ExtractedField],
    instructions: list[FillInstruction],
    run_id: str = "",
) -> list[FieldOutcome]:
```

Replace the `try/except` block inside the `execute_page` loop:

```python
try:
    strategy_name, _ = await _fill_field_with_fallback(page, field, inst.value)
    outcomes.append(
        FieldOutcome(
            field_id=field.id,
            action_taken="fill",
            value_used=inst.value,
            status="ok",
            error_message=None,
            strategy_used=strategy_name,
            failure_bundle_path=None,
        )
    )
except FillError as exc:
    strategy_errors: list[str] = str(exc).splitlines()
    bundle_path: str | None = None
    if run_id:
        bundle_path = await _capture_failure_bundle(page, field, inst, strategy_errors, run_id)
    outcomes.append(
        FieldOutcome(
            field_id=field.id,
            action_taken="fill",
            value_used=inst.value,
            status="fill_error",
            error_message=str(exc),
            strategy_used=None,
            failure_bundle_path=bundle_path,
        )
    )
```

---

### Step 5 — Thread `run_id` into `FormSubmissionStage` and refine the required-field gate

**File**: `src/autorole/stages/form_submission.py`

**5a. Thread `run_id` into `execute_page` call** (line 83):

```python
outcomes = await self._executor.execute_page(
    self._page, fields, instructions, run_id=ctx.run_id
)
```

**5b. Refine the existing `failed_outcomes` check** (lines 84–108).

Currently it blocks on ANY fill_error outcome. Change it to block only on required field failures:

```python
field_map = {f.id: f for f in fields}
required_failures = [
    o for o in outcomes
    if o.status in {"fill_error", "selector_not_found"}
    and field_map.get(o.field_id, None) is not None
    and field_map[o.field_id].required
]

if required_failures:
    failed_ids = ", ".join(o.field_id for o in required_failures)
    return StageResult.fail(
        f"Required field(s) could not be filled — refusing to advance. "
        f"failing_field_ids=[{failed_ids}]",
        "RequiredFieldFillError",
    )
```

Import `RequiredFieldFillError` from exceptions if you log it, but returning a `StageResult.fail`
with `"RequiredFieldFillError"` as the error_type string is sufficient — no need to raise.

---

## Testing Expectations

### Unit tests to add (new file: `tests/unit/test_extractor.py`)

- `test_classify_known_html_types`: `textarea`, `select`, `radio`, `checkbox`, `date`, `file`, `hidden`, plain `input` → correct FieldType
- `test_classify_aria_role_known`: `role="combobox"` → `"combobox_search"`, `role="listbox"` → `"select"`, etc.
- `test_classify_aria_role_unknown`: `role="grid"` → `"unknown"`
- `test_classify_contenteditable`: `contenteditable=True` with no ARIA role → `"textarea"`
- `test_build_selector_priority`: `data_automation_id` present → appears first; absent → ARIA label appears; none → name/id

### Unit tests to add (new file: `tests/unit/test_executor_strategies.py`)

- `test_strategy_ladder_uses_first_success`: mock strategies where first fails, second succeeds → returns second's name
- `test_strategy_ladder_exhausted_raises`: all strategies raise → `FillError` raised
- `test_capture_bundle_writes_files`: mock page, mock field → `field.json` + `context.html` + `screenshot.png` created
- `test_required_field_gate_blocks_required_failures`: `field.required=True` + `status="fill_error"` → `StageResult.fail`
- `test_required_field_gate_allows_optional_failures`: `field.required=False` + `status="fill_error"` → stage continues

### Existing integration tests

All existing tests under `tests/integration/` must pass without modification. The new fields on
`ExtractedField` and `FieldOutcome` all have defaults, so existing fixture JSON remains valid.

---

## What NOT to Do

- Do not modify `AIFieldMapper` or any LLM-calling code.
- Do not change `adapter` classes or `detector.py`.
- Do not add any `sleep` or `wait_for_timeout` calls inside strategy functions — the existing
  `wait_for(state="visible")` is sufficient.
- Do not swallow `FillError` silently anywhere — it must surface as `status="fill_error"` on the
  outcome so the stage can decide what to do.
- Do not implement item 3 stubs, imports, or placeholder files.
- Do not change the `_build_audit_log` or `_write_audit_log` functions — audit log structure is
  unchanged; the new `FieldOutcome` fields are silently ignored by the existing audit builder.
