from __future__ import annotations

import uuid

from autorole.integrations.form_controls.models import ExtractedField, FieldType


def _classify_field_type(tag: str, input_type: str) -> FieldType:
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


class SemanticFieldExtractor:
	def __init__(self, page: object) -> None:
		self._page = page

	async def extract(self, page_section: object, run_id: str, page_index: int) -> list[ExtractedField]:
		if not hasattr(self._page, "locator"):
			return []

		root_selector = getattr(page_section, "root", "body")
		page_label = getattr(page_section, "label", f"Page {page_index}")
		root = self._page.locator(root_selector)

		raw_fields = await root.locator("input, select, textarea").evaluate_all(
			"""
			els => els.map((el, i) => {
			  const tag = (el.tagName || '').toLowerCase();
			  const type = ((el.getAttribute('type') || '') + '').toLowerCase();
			  const name = el.getAttribute('name') || '';
			  const id = el.id || '';
			  const label = el.getAttribute('aria-label') ||
			    (el.labels && el.labels.length ? (el.labels[0].textContent || '').trim() : '') ||
			    el.getAttribute('placeholder') ||
			    name || id || `field_${i}`;
			  const required = el.required || el.getAttribute('aria-required') === 'true';
			  let options = [];
			  if (tag === 'select') {
			    options = Array.from(el.options || []).map(o => (o.textContent || '').trim()).filter(Boolean);
			  }
			  let prefilled = '';
			  if (tag === 'select') {
			    prefilled = (el.selectedOptions && el.selectedOptions[0] && (el.selectedOptions[0].textContent || '').trim()) || '';
			  } else if (type === 'checkbox' || type === 'radio') {
			    prefilled = el.checked ? (el.value || 'true') : '';
			  } else {
			    prefilled = (el.value || '').trim();
			  }
			  return { tag, type, name, id, label, required, options, prefilled, idx: i };
			})
			"""
		)

		fields: list[ExtractedField] = []
		for item in raw_fields:
			name = str(item.get("name") or "").strip()
			field_id_hint = name or str(item.get("id") or "").strip()
			if not field_id_hint:
				field_id_hint = f"field_{item.get('idx', len(fields))}"

			tag = str(item.get("tag") or "input").lower()
			input_type = str(item.get("type") or "").lower()
			field_type = _classify_field_type(tag, input_type)

			selector = f"[name={field_id_hint!r}], [id={field_id_hint!r}]"
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
					options=[str(opt) for opt in item.get("options", [])],
					prefilled_value=str(item.get("prefilled") or ""),
				)
			)

		return fields

