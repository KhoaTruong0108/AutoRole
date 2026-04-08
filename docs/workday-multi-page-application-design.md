# Workday Multi-Page Application Design

## 1. Objective

Support multi-page ATS application flows in `src/autorole_next/` using the existing stage loop:

1. `session`
2. `formScraper`
3. `fieldCompleter`
4. `formSubmission`
5. loop back to `formScraper` until the last page
6. final submit on the review page

The first target is Workday-style applications, where the candidate may move through pages such as:

1. Login or account creation
2. Applicant information
3. Applicant experience
4. Application questions
5. Voluntary disclosures
6. Self identify
7. Review and submit

The design must preserve the current SnapFlow topology and stage boundaries where practical. The goal is to make the existing loop reliable and page-aware, not to replace the pipeline with a different orchestration model.

## 2. Current State

The current implementation already contains the basic loop required for multi-page applications.

### 2.1 Existing loop behavior

Current stage flow:

1. `session` launches or prepares the shared browser
2. `formScraper` extracts fields from the current page
3. `fieldCompleter` generates fill instructions for those fields
4. `formSubmission` fills the page and calls the adapter to either:
   1. click next page
   2. click submit
   3. end in dry-run mode
5. `formSubmission` emits a `loop` decision to send control back to `formScraper`

This is already wired in:

1. `src/autorole_next/topology.py`
2. `src/autorole_next/gates/form_submission.py`
3. `src/autorole_next/executors/form_scraper.py`
4. `src/autorole_next/executors/form_submission.py`

### 2.2 Current limitations

The missing capabilities are not the loop itself. The missing capabilities are:

1. No first-class concept of page kind or page semantics
2. No first-class handling of Workday login or account creation in `session`
3. No page preparation step before scraping dynamic pages such as experience
4. No robust progress detection beyond a small fixed loop limit
5. No structured navigation result from the adapter
6. No way to distinguish normal fillable pages from review pages
7. No step-aware prompting or answering behavior in `fieldCompleter`

## 3. Design Goals

The design should satisfy the following requirements.

### 3.1 Functional goals

1. Support Workday-style multi-step applications without changing the high-level stage topology
2. Handle login and account creation before normal application form pages
3. Process each fillable page through `formScraper -> fieldCompleter -> formSubmission`
4. Support dynamic page preparation on pages that require clicking `Add` or similar controls before scraping
5. Recognize the final review page and submit only there
6. Preserve a complete cross-page audit trail

### 3.2 Non-goals for the first slice

1. No CAPTCHA solving
2. No email verification flow automation
3. No support for arbitrary deeply nested repeaters on day one
4. No rewrite of the generic extractor or filler stack unless required by a clear blocker
5. No new parallel apply runtime separate from the existing `autorole_next` stages

## 4. High-Level Flow

### 4.1 Target execution model

The target behavior is:

1. `session` owns platform bootstrap and authentication
2. `formScraper` owns page discovery, page preparation, and field extraction for the current application page
3. `fieldCompleter` owns answer generation for the extracted page fields
4. `formSubmission` owns filling, navigation, review-page submit, and loop control

### 4.2 Target sequence

```text
packaging
  -> session
  -> formScraper
  -> fieldCompleter
  -> formSubmission
      -> next_page  => formScraper
      -> submit     => concluding
      -> block      => DLQ / blocked
```

### 4.3 Page ownership model

Page ownership should be explicit.

1. `login_or_signup` is owned by `session`
2. `applicant_information` is owned by the normal form loop
3. `applicant_experience` is owned by the normal form loop, but requires adapter page preparation before scraping
4. `application_questions` is owned by the normal form loop
5. `voluntary_disclosures` is owned by the normal form loop
6. `self_identify` is owned by the normal form loop
7. `review` is owned by the normal form loop, but `formSubmission` treats it as terminal

## 5. Proposed Architecture Changes

### 5.1 Keep the current topology

Do not introduce a separate `formPage` stage or a parallel pipeline. The existing topology already fits the problem. The required change is to enrich the contracts between stages.

### 5.2 Add page-aware state

The current `form_session` dict tracks `page_index`, `all_fields`, `all_instructions`, `all_outcomes`, and `last_advance_action`. That is not enough for a real multi-page ATS flow.

`form_session` should become the canonical cross-page state object and include:

1. `page_index`
2. `current_step_key`
3. `current_step_label`
4. `current_step_kind`
5. `current_step_root`
6. `visited_steps`
7. `step_history`
8. `last_advance_action`
9. `last_navigation_result`
10. `pending_page_actions`
11. `performed_page_actions`
12. `final_review_ready`
13. `account_state`
14. `all_fields`
15. `all_instructions`
16. `all_outcomes`
17. `screenshots`

The intent is:

1. `page_index` remains a loop counter
2. `current_step_*` describes the live page
3. `visited_steps` and `step_history` protect against broken loops
4. `pending_page_actions` and `performed_page_actions` make dynamic preparation idempotent
5. `account_state` lets `session` report login or signup progress cleanly

### 5.3 Add page kind classification

Each page must be classified into one of these kinds:

1. `login_or_signup`
2. `applicant_information`
3. `applicant_experience`
4. `application_questions`
5. `voluntary_disclosures`
6. `self_identify`
7. `review`
8. `unknown`

The page kind must be detected by the ATS adapter, not by the generic scraper, because labels and DOM markers are platform-specific.

## 6. Adapter Contract Changes

### 6.1 Problem with the current adapter contract

The current adapter interface in `src/autorole_next/form_controls/adapters/base.py` is too small for multi-page support. It only covers:

1. `setup`
2. `get_current_page_section`
3. `advance`
4. `get_file_input`
5. `confirm_success`

That contract can click buttons, but it cannot explain what page is active, what preparation is needed, or whether navigation truly succeeded.

### 6.2 Proposed adapter extensions

Add richer adapter models and methods.

Suggested models:

```python
class StepInfo(BaseModel):
    key: str
    label: str
    kind: str
    root: str
    is_review: bool = False
    is_fillable: bool = True
    requires_preparation: bool = False


class PagePreparationResult(BaseModel):
    actions_requested: list[str] = []
    actions_performed: list[str] = []
    step_info: StepInfo


class NavigationResult(BaseModel):
    action: Literal["next_page", "submit", "done", "block"]
    from_step_key: str
    from_step_label: str
    next_step_key: str = ""
    next_step_label: str = ""
    next_step_kind: str = "unknown"
    transition_confirmed: bool = False
    reason: str = ""
```

Suggested new adapter methods:

1. `get_current_step_info(page) -> StepInfo`
2. `prepare_page_for_extraction(page, form_session) -> PagePreparationResult`
3. `advance(page, form_session) -> NavigationResult`
4. `bootstrap_authentication(page, credentials) -> AuthResult | None`

The adapter should still expose `get_file_input` and `confirm_success`.

### 6.3 Workday-specific behavior

The Workday adapter in `src/autorole_next/form_controls/adapters/workday.py` should implement:

1. step title detection from `currentStepTitle`
2. step kind inference from title text and Workday DOM markers
3. experience-page preparation by clicking `Add Education`, `Add Work Experience`, or similar controls before scrape
4. robust next-page detection after clicking `bottom-navigation-next-button`
5. final submit detection on the review page via `bottom-navigation-submit-button`

## 7. Session Stage Design

### 7.1 Ownership

`session` should own only the authentication and browser-bootstrap phase.

This is the correct boundary because:

1. login and account creation use predefined credentials
2. those credentials should never be generated or inferred by `fieldCompleter`
3. auth failure is operational, not a normal field-completion failure

### 7.2 Current problem

`src/autorole_next/executors/session.py` currently treats Workday as a public platform and does not attempt login or account creation.

That is incompatible with the target flow.

### 7.3 Required changes

`SessionExecutor` should:

1. launch the shared browser as it does today
2. connect to the page and inspect the current Workday screen
3. detect whether the page is:
   1. already on the first application step
   2. a sign-in page
   3. an account-creation page
4. if the page is auth-related, fill fixed credentials from config
5. submit the auth page
6. wait until the browser lands on the first fillable application page
7. write structured status into `payload["session"]`

### 7.4 Session payload additions

Add fields such as:

1. `auth_required`
2. `auth_mode`
3. `auth_completed`
4. `auth_blocked_reason`
5. `initial_step_key`
6. `initial_step_label`
7. `initial_step_kind`
8. `shared_browser`

### 7.5 Configuration changes

Add configuration under `src/autorole_next/config.py` for ATS credentials and bootstrap behavior.

Suggested config groups:

1. `multi_page.max_form_pages`
2. `multi_page.navigation_timeout_ms`
3. `multi_page.stuck_step_retries`
4. `workday.account_email`
5. `workday.account_username`
6. `workday.account_password`
7. `workday.enable_account_creation`
8. `workday.experience_autoreveal`

These values should be treated as operator configuration, not profile content.

## 8. Form Scraper Stage Design

### 8.1 New responsibilities

`formScraper` should become responsible for:

1. reading current step metadata from the adapter
2. preparing dynamic pages before extraction
3. extracting fields from the prepared page section
4. updating `form_session` with step metadata and performed preparation actions

### 8.2 New execution order

The target `FormScraperExecutor` flow is:

1. resolve page from the shared browser
2. if first iteration, reuse platform detection and initialize `form_session`
3. call `adapter.get_current_step_info(page)`
4. call `adapter.prepare_page_for_extraction(page, form_session)`
5. use the returned step root to build the page section
6. run `SemanticFieldExtractor.extract(...)`
7. store the extracted fields and updated step metadata into payload and `form_session`

### 8.3 Experience-page preparation

The applicant experience page is the first place where normal extraction is not enough. The page may hide education or work experience controls until the user clicks `Add`.

The design should be:

1. the adapter decides which repeatable blocks must be materialized
2. the adapter clicks the needed `Add` controls before scraping
3. the adapter records which actions were performed
4. the generic extractor only sees already-visible controls

This avoids teaching the generic extractor how to click buttons or create dynamic sections.

### 8.4 Idempotency rules

Page preparation must be safe across retries. If a stage reruns on the same page, it must not keep creating duplicate experience sections.

Use these rules:

1. store performed actions in `form_session.performed_page_actions`
2. only re-run an action if the page does not reflect the expected DOM state
3. validate that new fields became visible after the action
4. fail with a preparation error if required controls could not be exposed

## 9. Field Completer Stage Design

### 9.1 Current behavior

`fieldCompleter` maps extracted fields to answers using the current applicant profile and the LLM mapper.

This stage should remain in that role.

### 9.2 Required improvements

The mapper currently receives fields and page index, but not the semantic meaning of the current step. That is too weak for consistent behavior across:

1. experience pages
2. application questions
3. voluntary disclosures
4. self identify pages

`FieldCompleterExecutor` should pass these additional values into the mapping layer:

1. `current_step_kind`
2. `current_step_label`
3. `current_step_key`

### 9.3 Behavior by page kind

Expected behavior:

1. `applicant_information`: normal profile mapping
2. `applicant_experience`: map repeated education or work fields from bounded profile entries
3. `application_questions`: answer as current questionnaire behavior does, but with step-aware prompting
4. `voluntary_disclosures`: map from profile and explicit default policy
5. `self_identify`: map from profile and explicit default policy
6. `review`: normally no new answering logic, but allow checkboxes or final acknowledgements if present

### 9.4 Explicit boundary

`fieldCompleter` must not fill login credentials or create-account credentials. Those belong only to `session`.

## 10. Form Submission Stage Design

### 10.1 Current behavior

`formSubmission` currently:

1. validates required payloads
2. fills fields
3. uploads resume if a file input exists
4. calls `adapter.advance(page)`
5. if the adapter returns `next_page`, it loops
6. if the adapter returns `submit`, it confirms success

This is close to the needed behavior, but it needs stronger navigation and review semantics.

### 10.2 New responsibilities

`formSubmission` should become responsible for:

1. filling the current page
2. recording page completion metadata
3. invoking structured navigation
4. validating that navigation truly changed the step
5. routing back to `formScraper` only after successful progress
6. handling the review-page final submit path

### 10.3 Target execution order

1. read `current_step_kind` and `current_step_label` from `form_session`
2. fill the current page and collect outcomes
3. upload the packaged resume if needed
4. if the current page is `review`, verify final submit readiness
5. call `adapter.advance(page, form_session)`
6. inspect `NavigationResult`
7. if action is `next_page` and transition is confirmed, update `form_session` and emit loop
8. if action is `submit`, confirm success and emit pass
9. if action is `block`, fail with a specific business or navigation error

### 10.4 Review-page behavior

The review page is terminal.

Rules:

1. if editable controls are present, the normal scrape and fill flow may still run
2. before final submit, verify the submit button is visible and enabled
3. verify any required acknowledgements or consent controls are satisfied
4. perform final submit only from this page
5. store explicit review-step audit data

### 10.5 Progress detection

The current design increments `page_index` whenever action is `next_page` or `submit`. That is too optimistic.

Replace it with this rule:

1. increment `page_index` only when navigation is confirmed
2. compare `from_step_key` and `next_step_key`
3. if the step did not change, treat it as a navigation failure or stuck loop

## 11. Loop Control and Gate Design

### 11.1 Current problem

`src/autorole_next/gates/form_submission.py` defaults to `max_loops=2`.

That limit is too small for the target seven-step flow.

### 11.2 New loop strategy

Keep a maximum page guard, but do not rely on a tiny hard-coded number.

Use a combination of:

1. configurable `max_form_pages`
2. step-history progress detection
3. repeated-step detection
4. repeated-no-new-fields detection

### 11.3 Blocking rules

Block when:

1. the same step is observed repeatedly without progress
2. next-page navigation is reported but the step did not change
3. required dynamic controls could not be revealed
4. the flow remains on an auth screen after `session` claimed completion
5. the total page count exceeds configured safety thresholds

### 11.4 Decision model

`form_submission` payload should remain the gate input, but it should include:

1. `decision`
2. `reason`
3. `loop_count`
4. `current_step_key`
5. `current_step_kind`
6. `next_step_key`
7. `navigation_confirmed`
8. `progress_made`

## 12. Data Contract Changes

### 12.1 `session` payload

Suggested shape:

```json
{
  "platform": "workday",
  "authenticated": true,
  "auth_required": true,
  "auth_mode": "create_account",
  "auth_completed": true,
  "initial_step_key": "my-information",
  "initial_step_label": "My Information",
  "initial_step_kind": "applicant_information",
  "shared_browser": {"status": "ready"}
}
```

### 12.2 `formScraper` payload

Suggested additions:

```json
{
  "platform": "workday",
  "page_index": 1,
  "page_label": "My Experience",
  "step_key": "my-experience",
  "step_kind": "applicant_experience",
  "step_root": "[data-automation-id='formContainer']",
  "preparation_actions": ["add_work_experience_clicked"],
  "extracted_fields": []
}
```

### 12.3 `fieldCompleter` payload

Suggested additions:

```json
{
  "page_index": 1,
  "page_label": "My Experience",
  "step_key": "my-experience",
  "step_kind": "applicant_experience",
  "fill_instructions": []
}
```

### 12.4 `form_submission` payload

Suggested additions:

```json
{
  "decision": "loop",
  "reason": "advanced to next application page",
  "loop_count": 2,
  "current_step_key": "my-experience",
  "current_step_kind": "applicant_experience",
  "next_step_key": "application-questions",
  "next_step_kind": "application_questions",
  "navigation_confirmed": true,
  "progress_made": true,
  "submission_status": "rescrape_required"
}
```

## 13. Error Handling

### 13.1 New error classes or categories

Introduce or standardize error categories for:

1. `AuthenticationError`
2. `AccountCreationError`
3. `PagePreparationError`
4. `NavigationStuckError`
5. `UnexpectedStepError`
6. `ReviewSubmitReadinessError`

### 13.2 Failure semantics

Use these rules:

1. auth errors fail in `session`
2. extraction preparation errors fail in `formScraper`
3. answering or mapping errors fail in `fieldCompleter`
4. navigation or final-submit confirmation errors fail in `formSubmission`

Do not silently fall back from a broken multi-page flow into a fake submit success.

## 14. Audit and Observability

### 14.1 Existing audit behavior

`formSubmission` already writes audit logs and screenshots.

### 14.2 Required additions

Add cross-page observability for:

1. `step_history`
2. step labels and kinds
3. navigation results
4. page preparation actions
5. per-step screenshots
6. auth bootstrap events from `session`

### 14.3 Desired audit outcome

When a run fails, an operator should be able to answer:

1. which step the run reached
2. which step failed
3. whether the page was prepared correctly
4. whether navigation progressed
5. whether final submit was attempted or blocked

## 15. File-Level Change Plan

### 15.1 `src/autorole_next/executors/session.py`

Change from simple shared-browser setup to platform bootstrap owner for login and account creation.

Required updates:

1. connect to the shared browser page after launch
2. detect Workday auth or first-step state
3. fill configured credentials when needed
4. wait for first application step
5. persist structured auth outcome in payload and store

### 15.2 `src/autorole_next/form_controls/adapters/base.py`

Expand the interface with:

1. step info model
2. page preparation result model
3. structured navigation result model
4. optional auth bootstrap hook

### 15.3 `src/autorole_next/form_controls/adapters/workday.py`

Implement Workday-specific step handling:

1. map Workday step titles to page kinds
2. add page preparation for experience sections
3. detect terminal review page
4. confirm next-page transitions
5. improve submit confirmation handling

### 15.4 `src/autorole_next/executors/form_scraper.py`

Update the scraper to:

1. read step info
2. run page preparation
3. store page metadata
4. update cross-page session state

### 15.5 `src/autorole_next/executors/field_completer.py`

Pass step metadata into the mapping layer and keep login credentials out of it.

### 15.6 `src/autorole_next/executors/form_submission.py`

Update submission to:

1. use structured navigation
2. confirm progress before looping
3. treat review as terminal
4. emit richer gate payloads

### 15.7 `src/autorole_next/gates/form_submission.py`

Replace the fixed low loop ceiling with configurable page limits plus progress-aware blocking.

### 15.8 `src/autorole_next/config.py`

Add configuration for:

1. ATS credentials
2. page navigation limits
3. navigation timeouts
4. Workday multi-page behavior toggles

## 16. Testing Strategy

### 16.1 Unit tests

Add focused unit coverage for:

1. session auth bootstrap success and failure
2. Workday step classification
3. experience-page `Add` preparation behavior
4. `formScraper` state updates across pages
5. `fieldCompleter` step-aware mapping inputs
6. `formSubmission` navigation confirmation and review submit
7. `form_submission` gate blocking on no-progress loops

Primary test targets:

1. `tests/unit/autorole_next/test_form_submission_executor.py`
2. `tests/unit/autorole_next/test_form_submission_gate.py`
3. new session and Workday adapter unit tests under `tests/unit/autorole_next/`

### 16.2 Integration tests

Extend the existing slices to cover a full multi-page flow:

1. session establishes or creates the account
2. applicant information page loops successfully
3. experience page reveals dynamic sections and loops successfully
4. questions, disclosures, and self-identify loop successfully
5. review page submits and confirms success

Primary integration files:

1. `tests/integration/autorole_next/test_form_scraper_slice.py`
2. `tests/integration/autorole_next/test_form_submission_slice.py`

### 16.3 Verification command

When implementation starts, validate with the repo's existing `PYTHONPATH=src` convention for `autorole_next` tests.

## 17. Rollout Plan

Implement in this order.

### Phase 1: Contracts and state

1. extend adapter interfaces and models
2. enrich `form_session`
3. add step metadata plumbing across stages

### Phase 2: Session bootstrap

1. add config for Workday credentials
2. implement Workday auth bootstrap in `session`
3. add unit tests for auth handling

### Phase 3: Scraper page preparation

1. add `prepare_page_for_extraction`
2. implement experience-page reveal behavior
3. add unit tests for dynamic preparation

### Phase 4: Submission and gating

1. add structured navigation results
2. add progress confirmation and review submit logic
3. raise loop ceiling and add stuck-loop blocking

### Phase 5: End-to-end coverage

1. extend integration slices
2. verify audit outputs
3. tune selectors and timeouts from real runs

## 18. Key Decisions

### 18.1 Keep login in `session`

Login and account creation must stay in `session`, not inside the generic page loop.

Reason:

1. the credentials are predefined
2. the behavior is platform bootstrap, not applicant data completion
3. auth failures should stop early with a clear operational error

### 18.2 Keep experience `Add` logic in the adapter

Experience-page reveal actions must live in the Workday adapter, not in the generic extractor.

Reason:

1. the controls are Workday-specific
2. extractor responsibilities should stay read-oriented
3. adapter preparation gives retry-safe, page-specific behavior

### 18.3 Keep the stage loop

Do not replace the existing `formScraper -> fieldCompleter -> formSubmission -> formScraper` loop.

Reason:

1. it already matches the desired multi-page behavior
2. most required work is better contracts and better state
3. replacing the topology would create risk without clear benefit

## 19. Open Questions

These questions can be left for implementation-time refinement, but they should be tracked.

1. How many education and work entries should be materialized by default on the experience page?
2. Should voluntary disclosure and self-identify answers come entirely from profile data, or should there be explicit policy defaults in config?
3. Should review pages allow one last scrape-fill cycle for consent checkboxes, or should they be treated as mostly read-only?
4. Should repeated-step detection use step label only, or label plus DOM root fingerprint?

## 20. Summary

The existing `autorole_next` pipeline already has the right loop shape for multi-page applications. The required design change is to make the loop page-aware and platform-aware.

The key structural changes are:

1. `session` owns Workday login and account creation
2. adapters classify steps and prepare pages before scraping
3. `formScraper` captures richer step metadata
4. `fieldCompleter` becomes step-aware
5. `formSubmission` becomes a confirmed navigator instead of a blind next-clicker
6. the gate blocks on lack of progress, not on an unrealistically small loop limit

With those changes, the existing stage loop can support the full Workday-style sequence from account creation through review and final submit.