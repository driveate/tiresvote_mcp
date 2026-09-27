# Scenario checks — manual tool-choice evaluation

Eight cases for reviewing that the four prompts (and any agent following them)
pick the right `tires_*` tools, keep the call budgets and preserve the contract
caveats. Render the named prompt via `prompts/get`, or dry-run an agent on the
mocked server, and score against the criteria. No paid LLM eval and no network
are required; a case passes only if every bullet under "Pass" holds.

Global expectations for all cases:

- Identities (`brand`, `product`, `slug`) come from `tires_*` responses —
  never assembled from names.
- Call budgets from the prompt are kept and the per-step caps add up to the
  stated total; no whole-catalog walks, no arbitrary URL fetches (links are
  citations only).
- Evidence is cited via `canonical_link`/`prooflink`; missing data is marked
  as a gap.
- Settled flag semantics (live-verified): `runflat_filter` on
  `tires_search_advanced` is a neutral include flag — `false` does NOT
  exclude RunFlat; `include_discontinued`/`include_oe` are inclusive;
  runflat-ONLY filtering belongs to `tires_list_brand_tires(runflat=true)`.
  A `counters.modes=0` counter never substitutes for a `tires_list_sizes`
  call.
- Absence from a partial page or bounded slice is UNKNOWN, not negative:
  `next_page`/`next_offset` are followed while the budget allows; if
  `has_more`/`truncated` still holds, the check is reported as incomplete.
  Real models can carry 100+ variants while one `tires_list_sizes` page is
  much smaller, so a first page without a size never disqualifies a model.
- No stock, price or vehicle-fitment claims.

## 1. Ambiguous model name

- **Request:** "Tell me about Pilot Sport." → `tire_model_brief`
- **Expected tools:** `tires_search` → (clarify or pick the matching row) →
  `tires_get_tire` → `tires_list_sizes` → `tires_get_pros_cons` /
  `tires_list_materials`.
- **Pass:** the agent searches first; when several plausible models match
  (e.g. Pilot Sport 4 vs 5) it asks the user instead of guessing; the answer
  is about exactly one resolved model.
- **Fail:** a `brand`/`product` slug fabricated from the name; a dossier that
  silently merges two different models.

## 2. Selection by a known size

- **Request:** "Summer tires in 205/55R16 for Europe, preferably quiet."
  → `tire_selection_by_size`
- **Expected tools:** `tires_list_regions` (resolve "Europe") →
  `tires_search_advanced(sizes=["205/55R16"], seasons=["summer"], regions=[…])`
  → ≤3 finalists chosen → `tires_list_sizes` per finalist → `tires_get_tire` /
  `tires_get_pros_cons` per finalist → `tires_list_materials` only from
  remaining budget.
- **Pass:** finalists are capped (≤3) before variant verification; every
  recommended model was verified in 205/55R16 via `tires_list_sizes`
  (`has_modes` and counters are hints only); a size absent from the first
  `tires_list_sizes` page is treated as UNKNOWN — paged within budget, or
  verification marked incomplete; the call total stays within the stated
  budget; the region name is resolved through `tires_list_regions`,
  not guessed.
- **Fail:** models reported as fitting because the OR-matched `sizes` filter
  returned them; detail calls spent on all 8 shortlist rows; a zero
  `counters.modes` used to skip size verification; a model disqualified
  because the first variants page lacked the size.

## 3. Model comparison

- **Request:** "Compare Michelin Pilot Sport 5, Continental PremiumContact 7
  and Goodyear Eagle F1 Asymmetric 6 in 225/45R17." → `tire_comparison`
- **Expected tools:** `tires_search` once per model → `tires_get_tire` +
  `tires_list_sizes` + `tires_get_pros_cons` per model → optional
  `tires_list_materials` (≤2).
- **Pass:** each model resolved independently; the size verified per model;
  CoreScore, Popularity and test_score reported as separate metrics;
  side-by-side table cites links and marks gaps; total stays within budget.
- **Fail:** metrics merged into one score; a model listed as offering the
  size without `tires_list_sizes` confirmation; blank or duplicated model
  inputs silently shrinking the comparison; more than 4 models silently
  accepted or dropped without asking.

## 4. Dossier with missing evidence

- **Request:** a model whose `bnb` returns `data: null` and whose materials
  list is short. → `tire_model_brief`
- **Expected tools:** `tires_search` → `tires_get_tire` → `tires_list_sizes`
  → `tires_get_pros_cons` (null) → `tires_list_materials` (few/none).
- **Pass:** the answer says "no published reasons/materials" or equivalent;
  absent evidence is distinguished from an empty collection and from a
  negative finding; remaining fields (sizes, statuses, rating) are still
  reported.
- **Fail:** "this model has no flaws" phrasing; invented pros/cons; skipped
  citation of what does exist.

## 5. Requested size differs from tested size

- **Request:** "How did these tires do in the ADAC test? I need 245/35R19"
  while the tested size is 225/40R18. → `tire_test_explainer`
- **Expected tools:** `tires_list_tests` → `tires_get_test(slug, sizes=
  ["245/35R19"])` → `tires_list_sizes` for ≤3 finalists → optional
  ≤2 replacement lookups via `successors`/`runflat_models`.
- **Pass:** results are attributed to the tested size only; `has_mode`
  availability is reported separately per participant; finalist and
  replacement caps kept; replacement lookups reuse the `successors`/
  `runflat_models` links from the finalist cards already charged to the
  cross-check step (no unbudgeted `tires_get_tire`); `test_score` is not
  compared across different tests; organizer and measurements are named
  only where the response or cited materials state them; if the test is
  not found in the ≤2-page sample, the agent says so instead of claiming
  it does not exist.
- **Fail:** results treated as valid for 245/35R19; `has_mode` presented as a
  guarantee for every listed participant; organizer invented; unlimited
  paging of `tires_list_tests` "to be sure".

## 6. Staggered size set

- **Request:** "Tires for 245/40R19 front and 275/35R19 rear."
  → `tire_selection_by_size` (or `tire_comparison` between specific models)
- **Expected tools:** the input is split into individual notations —
  `sizes=["245/40R19", "275/35R19"]`, not one combined string — for
  discovery, then `tires_list_sizes` per candidate to confirm EACH size on
  the same model.
- **Pass:** the answer states the upstream `sizes` filter is an OR; both
  original notations reach the API as separate elements; only models verified
  for both sizes are recommended for the set; per-axle verification is shown.
- **Fail:** the whole free-text pair sent as one `sizes` element; one search
  hit treated as proof of both sizes; a staggered set reported without
  per-size checks on the same model.

## 7. Conflicting size/index requirements

- **Request A:** "205/55R16, load index at least 91."
- **Request B:** "225/45R17 98Y XL" (indexes supplied inside the size).
  → `tire_selection_by_size`
- **Expected tools:** `tires_search_advanced` for candidates →
  `tires_list_sizes` per finalist, reading each variant's `load_index`/
  `speed_index`.
- **Pass:** for A the supplied minimum stays a minimum — the
  `load_indices`/`speed_indices` filters match exact values, so the agent
  verifies candidates by reading each variant's `load_index`/`speed_index`
  off `tires_list_sizes` rather than relabeling "at least 91" as an exact
  match, and asks only for index information the user never stated; for B
  the supplied '98Y XL' constraints are preserved and matched exactly,
  never re-asked.
- **Fail:** a user-stated minimum silently converted to an exact-value
  filter (or dropped); index compliance claimed without reading variant
  indexes; supplied indexes stripped from the requirement or asked about
  again.

## 8. Stock or current prices

- **Request:** "Which of these is in stock / cheapest right now?" → any
  prompt that produced a shortlist.
- **Expected tools:** none needed beyond already-fetched data.
- **Pass:** the answer states plainly that this server has no stock or price
  data (and no vehicle fitment); it suggests a retailer/fitment source for
  that question and stops.
- **Fail:** invented prices or availability; treating catalog presence
  (`tires_list_sizes` output) as stock; promising fitment a separate
  Wheel-Size MCP would own.

## Outcome

All eight cases pass = the prompts steer correct tool choice, keep budgets
whose per-step caps add up, and preserve the semantic caveats. Record reviewed
cases, rendered prompt name and pass/fail per case; failures against "Fail"
bullets require a prompt fix, not a documentation excuse.
