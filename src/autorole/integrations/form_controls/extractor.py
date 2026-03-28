from __future__ import annotations

import re
import uuid

from autorole.integrations.form_controls.models import ExtractedField, FieldType


_POPUP_OPTION_SELECTOR = (
	'[role="option"], '
	'[role="listbox"] [role="option"], '
	'[role="menuitem"], '
	'[role="menu"] [role="menuitem"]'
)

# ---------------------------------------------------------------------------
# Strategy B — static option catalog for known platform EEO / consent fields.
# Keys are lowercase substrings matched against the normalized field label.
# ---------------------------------------------------------------------------
_PLATFORM_OPTION_CATALOG: dict[str, dict[str, list[str]]] = {
    "greenhouse": {
        "gender": [
            "Male",
            "Female",
            "Non-binary",
            "Other",
            "I don't wish to answer",
            "Decline to self-identify",
        ],
        "race": [
            "American Indian or Alaskan Native",
            "Asian",
            "Black or African American",
            "Hispanic or Latino",
            "White",
            "Native Hawaiian or Other Pacific Islander",
            "Two or more races",
            "I don't wish to answer",
            "Decline to self-identify",
        ],
        "veteran": [
            "I am not a protected veteran",
            "I identify as one or more of the classifications of a protected veteran",
            "I don't wish to answer",
        ],
        "disability": [
            "Yes, I have a disability, or have had one in the past",
            "No, I don't have a disability and haven't had one in the past",
            "I don't wish to answer",
        ],
        "hispanic": ["Yes", "No", "I don't wish to answer"],
        "authorized": ["Yes", "No"],
        "sponsorship": ["Yes", "No"],
        "require": ["Yes", "No"],
    },
}


def _seed_options_from_catalog(label: str, platform_id: str) -> list[str]:
    """Return known options for the field from the static catalog, or [] if no match."""
    catalog = _PLATFORM_OPTION_CATALOG.get(platform_id)
    if not catalog:
        return []
    normalized = label.lower().strip()
    for keyword, options in catalog.items():
        if keyword in normalized:
            return list(options)
    return []


def _normalize_option_text(text: str) -> str:
	collapsed = " ".join(text.split())
	return re.sub(r"\s*\+(\d+)\s*$", r" +\1", collapsed)


def _unique_options(options: list[str]) -> list[str]:
	seen: set[str] = set()
	unique: list[str] = []
	for option in options:
		normalized = _normalize_option_text(option)
		if not normalized or normalized in seen:
			continue
		seen.add(normalized)
		unique.append(normalized)
	return unique


async def _collect_option_texts(page: object, control_ids: list[str]) -> list[str]:
	if hasattr(page, "evaluate"):
		try:
			texts: list[str] = await page.evaluate(
				"""
				controlIds => {
				  const OPTION_SELECTOR = '[role="option"], [role="menuitem"]';
				  const isVisible = el => {
				    const style = window.getComputedStyle(el);
				    return !!style && style.visibility !== 'hidden' && style.display !== 'none' && el.getClientRects().length > 0;
				  };
				  const roots = controlIds
				    .map(id => document.getElementById(id))
				    .filter(root => root);
				  const nodes = roots.length
				    ? roots.flatMap(root => Array.from(root.querySelectorAll(OPTION_SELECTOR)))
				    : Array.from(document.querySelectorAll(OPTION_SELECTOR));
				  return Array.from(new Set(
				    nodes
				      .filter(isVisible)
				      .map(el => (el.innerText || el.textContent || '').trim())
				      .filter(Boolean)
				  ));
				}
				""",
				control_ids,
			)
		except Exception:
			texts = []
		if texts:
			return _unique_options(texts)

	texts: list[str] = []
	for control_id in control_ids:
		container = page.locator(f'#{control_id}').first  # type: ignore[union-attr]
		try:
			if await container.count() == 0:
				continue
			text_block = await container.inner_text()
		except Exception:
			continue
		texts.extend(part.strip() for part in text_block.splitlines() if part.strip())

	if texts:
		return _unique_options(texts)

	try:
		all_texts: list[str] = await page.locator(_POPUP_OPTION_SELECTOR).all_text_contents()  # type: ignore[union-attr]
	except Exception:
		return []
	return _unique_options(all_texts)


async def _load_lazy_options(page: object, field: ExtractedField) -> list[str]:
	"""
	Strategy A — open the combobox, read rendered options, Escape to close.
	Returns [] on any failure; never raises.
	"""
	try:
		loc = page.locator(field.selector).first  # type: ignore[union-attr]
		control_ids: list[str] = []
		if hasattr(loc, "evaluate"):
			try:
				control_ids = await loc.evaluate(
					"el => [el.getAttribute('aria-controls'), el.getAttribute('aria-owns')].filter(Boolean)"
				)
			except Exception:
				control_ids = []
		if hasattr(loc, "scroll_into_view_if_needed"):
			await loc.scroll_into_view_if_needed(timeout=2_000)
		await loc.click(timeout=3_000)
		try:
			await page.wait_for_selector(_POPUP_OPTION_SELECTOR, timeout=1_200)  # type: ignore[union-attr]
		except Exception:
			if hasattr(loc, "press"):
				try:
					await loc.press("ArrowDown")
					await page.wait_for_selector(_POPUP_OPTION_SELECTOR, timeout=1_200)  # type: ignore[union-attr]
				except Exception:
					pass

		texts = await _collect_option_texts(page, control_ids)
		if not control_ids and len(texts) > 25 and "country" not in field.label.lower():
			return []
		await page.keyboard.press("Escape")  # type: ignore[union-attr]
		if hasattr(page, "wait_for_timeout"):
			await page.wait_for_timeout(150)  # type: ignore[union-attr]
		return texts
	except Exception:
		return []


_ARIA_ROLE_MAP: dict[str, FieldType] = {
	"combobox": "combobox_search",
	"listbox": "select",
	"radio": "radio",
	"checkbox": "checkbox",
	"switch": "checkbox",
	"spinbutton": "text",
	"searchbox": "text",
	"textbox": "text",
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
	if tag == "textarea":
		return "textarea"
	if tag == "select":
		return "select"
	if input_type == "radio":
		return "radio"
	if input_type == "checkbox":
		return "checkbox"
	if input_type == "date":
		return "date"
	if input_type == "file":
		return "file"
	if input_type == "hidden":
		return "hidden"
	return "text"


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


class SemanticFieldExtractor:
	def __init__(self, page: object) -> None:
		self._page = page

	async def extract(
		self,
		page_section: object,
		run_id: str,
		page_index: int,
		platform_id: str = "",
	) -> list[ExtractedField]:
		if not hasattr(self._page, "locator"):
			return []

		root_selector = getattr(page_section, "root", "body")
		page_label = getattr(page_section, "label", f"Page {page_index}")
		root = self._page.locator(root_selector)
		try:
			if await root.count() == 0 and root_selector != "body":
				root = self._page.locator("body")
			if await root.count() == 0:
				return []
			root_handle = await root.evaluate_handle("el => el")
		except Exception:
			return []

		raw_fields = await self._page.evaluate(
			"""
			rootEl => {
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

			  function collectElements(rootNode, fromShadow) {
			    const results = [];
			    rootNode.querySelectorAll(INTERACTIVE).forEach(el => results.push({ el, fromShadow }));
			    rootNode.querySelectorAll('*').forEach(el => {
			      if (el.shadowRoot) {
			        collectElements(el.shadowRoot, true).forEach(r => results.push(r));
			      }
			    });
			    return results;
			  }

			  return collectElements(rootEl, false).map(({ el, fromShadow }, i) => {
			    const tag = (el.tagName || '').toLowerCase();
			    const type = (el.getAttribute('type') || '').toLowerCase();
			    const role = el.getAttribute('role') || '';
			    const name = el.getAttribute('name') || '';
			    const id = el.id || '';
			    const dataAutomationId = el.getAttribute('data-automation-id') || '';
			    const dataTestId = el.getAttribute('data-testid') || '';
			    const contentEditable = el.getAttribute('contenteditable') === 'true';
			    const insideCombobox = !!el.closest('[role="combobox"]');
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
			    return {
			      tag,
			      type,
			      role,
			      name,
			      id,
			      dataAutomationId,
			      dataTestId,
			      contentEditable,
			      label,
			      required,
			      options,
			      prefilled,
			      fromShadow,
			      insideCombobox,
			      idx: i,
			    };
			  });
			}
			""",
			root_handle,
		)
		if hasattr(root_handle, "dispose"):
			await root_handle.dispose()

		# Deduplicate by stable DOM identity. If duplicate exists, prefer shadow-origin entries.
		by_key: dict[tuple[str, str, str], dict[str, object]] = {}
		ordered_items: list[dict[str, object]] = []
		for item in raw_fields:
			name = str(item.get("name") or "").strip()
			id_ = str(item.get("id") or "").strip()
			data_automation_id = str(item.get("dataAutomationId") or "").strip()
			key = (name, id_, data_automation_id)
			if key == ("", "", ""):
				ordered_items.append(item)
				continue
			existing = by_key.get(key)
			if existing is None:
				by_key[key] = item
				ordered_items.append(item)
				continue
			if bool(item.get("fromShadow", False)) and not bool(existing.get("fromShadow", False)):
				by_key[key] = item
				index = ordered_items.index(existing)
				ordered_items[index] = item

		fields: list[ExtractedField] = []
		for item in ordered_items:
			name = str(item.get("name") or "").strip()
			id_ = str(item.get("id") or "").strip()
			data_automation_id = str(item.get("dataAutomationId") or "").strip()
			data_testid = str(item.get("dataTestId") or "").strip()
			tag = str(item.get("tag") or "input").lower()
			input_type = str(item.get("type") or "").lower()
			aria_role = str(item.get("role") or "").lower()
			contenteditable = bool(item.get("contentEditable", False))
			from_shadow = bool(item.get("fromShadow", False))
			inside_combobox = bool(item.get("insideCombobox", False))

			field_id_hint = name or id_ or data_automation_id or f"field_{item.get('idx', len(fields))}"
			field_type = _classify_field_type(tag, input_type, aria_role, contenteditable)
			selector = _build_selector(name, id_, str(item.get("label") or ""), data_automation_id, data_testid)

			if selector == "body":
				continue
			if (
				field_type == "text"
				and inside_combobox
				and not any((name, id_, data_automation_id, data_testid))
				and str(item.get("label") or "").startswith("field_")
			):
				continue

			stable_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{page_index}:{field_id_hint}:{selector}"))
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
					options=_unique_options([str(opt) for opt in item.get("options", [])]),
					prefilled_value=str(item.get("prefilled") or ""),
					aria_role=aria_role,
					extraction_source="shadow_dom" if from_shadow else "dom",
				)
			)

		# Post-pass: enrich options for any combobox field that has none.
		# All comboboxes arrive with role="combobox" → classified as combobox_search.
		# We detect the true type by behavior: click reveals options immediately → lazy.
		# Order: B (static catalog, free) → A (click-and-detect, browser interaction).
		enriched: list[ExtractedField] = []
		for field in fields:
			if field.field_type not in ("combobox_lazy", "combobox_search") or field.options:
				enriched.append(field)
				continue

			# Strategy B: static catalog match (fast, no browser interaction).
			seeded = _seed_options_from_catalog(field.label, platform_id)
			if seeded:
				enriched.append(field.model_copy(update={"options": seeded, "field_type": "combobox_lazy"}))
				continue

			# Strategy A: click-and-detect. Options appear immediately → lazy; timeout → search.
			loaded = await _load_lazy_options(self._page, field)
			if loaded:
				enriched.append(field.model_copy(update={"options": loaded, "field_type": "combobox_lazy"}))
			else:
				enriched.append(field)  # confirmed combobox_search: options load only after typing

		return enriched

