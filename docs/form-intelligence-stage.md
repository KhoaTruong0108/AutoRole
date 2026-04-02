# FormIntelligenceStage Design Notes

## Questionnaire Format

Questionnaire items follow the Appendix format:

- Q header: ## Q{n}
- Map field: mapping_type:field_name:value_type
- Question: human-readable label
- Options list: explicit choices or (free text)
- Answer: initially empty, later populated by AI

Example map values:

- direct:email:value
- direct:country:choice
- direct:work_authorisation:choice

## Map Field Parsing

Map strings are structured as:

- mapping_type: currently direct
- field_name: target field id/name in form JSON
- value_type:
  - value for free-text/file values
  - choice for single/multi-choice values

During merge, map is reconstructed from each source field and used as the lookup key for AI answers.

## Building Questionnaire from Raw Form JSON

_build_questionnaire reads form_json_raw[fields], each item including:

- id
- label
- type
- required
- options

Conversion rules:

- single_choice and multiple_choice -> value_type=choice
- all other types -> value_type=value
- answer starts as empty string

## AI Answering and Merge Strategy

The stage sends rendered questionnaire text plus user profile context to the LLM.

Expected response model:

- answers: list of {map, answer}
- unanswered_required: map keys that could not be answered

Merge process:

- Build answers_by_map dictionary
- For each field, reconstruct map key
- If answer exists:
  - multiple_choice: split comma-separated answer into list values
  - otherwise write string into field value

## CAPTCHA Detection and Solving Flow

Detection checks page HTML for known challenge markers:

- recaptcha
- hcaptcha
- cf-challenge

Flow:

1. Navigate to listing URL.
2. Detect CAPTCHA.
3. If none, continue.
4. If detected and no solver: fail with CaptchaChallenge.
5. If solver exists: attempt solve up to MAX_CAPTCHA_ATTEMPTS.
6. If all attempts fail: block with CaptchaChallenge.
7. If solved: proceed to form extraction and answering.
