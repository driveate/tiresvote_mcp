"""Offline checks for the TiresVote prompt module.

Each test registers ``prompts`` on a standalone FastMCP instance and talks to
it through the in-memory ``Client`` — no network, no ``tiresvote_mcp.server``
dependency.
"""

import re

import pytest
from fastmcp import Client, FastMCP

from tiresvote_mcp import prompts

EXPECTED_NAMES = {
    "tire_selection_by_size",
    "tire_comparison",
    "tire_test_explainer",
    "tire_model_brief",
}

CYRILLIC = re.compile(r"[\u0400-\u04ff]")
BUDGET = re.compile(r"call budget|at most|≤\s*\d+", re.IGNORECASE)


@pytest.fixture
def server() -> FastMCP:
    mcp = FastMCP("tiresvote-prompts-test")
    prompts.register(mcp)
    return mcp


async def render(client: Client, name: str, arguments: dict[str, str]) -> str:
    result = await client.get_prompt(name, arguments)
    assert result.messages, f"{name} produced no messages"
    text = "\n".join(getattr(m.content, "text", "") for m in result.messages)
    assert text.strip()
    assert not CYRILLIC.search(text), f"{name} rendered non-English text"
    # collapse wrap so phrase checks do not depend on line breaks
    return re.sub(r"\s+", " ", text)


@pytest.mark.asyncio
async def test_exactly_the_four_approved_names(server):
    async with Client(server) as client:
        listed = await client.list_prompts()
    assert {p.name for p in listed} == EXPECTED_NAMES


@pytest.mark.asyncio
async def test_arguments_are_typed_and_documented(server):
    async with Client(server) as client:
        meta = {p.name: p for p in await client.list_prompts()}

    for prompt in meta.values():
        assert prompt.description, f"{prompt.name} lacks a description"
        assert prompt.arguments, f"{prompt.name} has no arguments"
        for arg in prompt.arguments:
            assert arg.description, f"{prompt.name}.{arg.name} lacks help"

    comparison = {a.name: a for a in meta["tire_comparison"].arguments}
    assert comparison["model_1"].required
    assert comparison["model_2"].required
    assert not comparison["model_3"].required
    assert not comparison["model_4"].required
    assert "model_5" not in comparison  # scope is capped at 4

    selection = {a.name: a for a in meta["tire_selection_by_size"].arguments}
    assert selection["size"].required
    assert not selection["season"].required
    assert not selection["region"].required


@pytest.mark.asyncio
async def test_selection_plan_finalists_before_verification(server):
    async with Client(server) as client:
        text = await render(
            client,
            "tire_selection_by_size",
            {
                "size": "225/45R17",
                "season": "summer",
                "region": "europe",
                "priorities": "low noise",
            },
        )
    assert "225/45R17" in text
    assert "summer" in text and "europe" in text and "low noise" in text
    assert "tires_search_advanced" in text
    assert "tires_list_sizes" in text
    # concrete plan: shortlist bound, finalists capped before verification
    assert "≤8" in text  # shortlist bound
    assert re.search(r"≤3 finalists", text)
    assert "≤16" in text  # stated total matches the per-step caps
    assert re.search(r"remaining budget", text, re.IGNORECASE)
    assert re.search(r"one element per notation|individual size notations", text)
    assert re.search(r"\bOR\b", text)  # sizes filter = alternatives
    assert re.search(r"\bEVERY\b|\beach\b", text, re.IGNORECASE)
    # settled live contract: neutral runflat flag, inclusive np/oe;
    # no leftover "awaits live verdict" language
    assert "runflat_filter" in text
    assert "include_runflat" not in text
    assert not re.search(
        r"await|pending|unconfirmed|preliminary|live.{0,20}verdict",
        text,
        re.IGNORECASE,
    )
    # counters.modes=0 cannot skip verification (live evidence)
    assert "counters.modes=0" in text
    # a size absent from the first list_sizes page is UNKNOWN, not negative:
    # page within budget, truncation left -> incomplete verification
    assert re.search(r"UNKNOWN", text)
    assert re.search(r"incomplete", text, re.IGNORECASE)
    # indexes: supplied minimums stay minimums, filters are exact equality
    assert re.search(r"at least 91", text)
    assert re.search(r"minimum", text, re.IGNORECASE)
    assert re.search(r"exact values", text, re.IGNORECASE)
    assert "null" in text  # absent evidence is not a negative finding


@pytest.mark.asyncio
async def test_comparison_bounds_and_metric_separation(server):
    async with Client(server) as client:
        text = await render(
            client,
            "tire_comparison",
            {
                "model_1": "Michelin Pilot Sport 5",
                "model_2": "Continental PremiumContact 7",
                "model_3": "Goodyear Eagle F1 Asymmetric 6",
                "size": "225/45R17",
                "criteria": "wet grip",
            },
        )
    for name in (
        "Michelin Pilot Sport 5",
        "Continental PremiumContact 7",
        "Goodyear Eagle F1 Asymmetric 6",
    ):
        assert name in text
    assert "comparing 3 tire models" in text  # counted from supplied names
    assert "tires_search" in text  # names resolved, not slugs inferred
    assert "tires_list_sizes" in text
    assert re.search(r"slug", text, re.IGNORECASE)
    assert re.search(r"2-4", text)  # documented scope bound
    assert re.search(r"≤4 calls", text)  # one resolution call per model
    assert "≤18" in text  # feasible total: 4 search + 12 per-model + 2 materials
    assert "CoreScore" in text and "Popularity" in text
    assert "test_score" in text  # distinct metrics stay distinct
    assert "null" in text
    # supplied indexes stay part of the requirement; bare size clarified only
    assert re.search(r"bare geometric", text, re.IGNORECASE)
    assert re.search(r"do not re-ask", text, re.IGNORECASE)


@pytest.mark.asyncio
async def test_comparison_rejects_blank_required_models(server):
    async with Client(server) as client:
        with pytest.raises(Exception):  # blank name must not render
            await client.get_prompt("tire_comparison", {"model_1": "   ", "model_2": "Valid Model"})


@pytest.mark.asyncio
async def test_comparison_filters_blank_optional_models(server):
    async with Client(server) as client:
        text = await render(
            client,
            "tire_comparison",
            {"model_1": "Model A", "model_2": "Model B", "model_4": "Model D"},
        )
    # model_4 without model_3 collapses to a 3-way comparison, no gap
    assert "comparing 3 tire models" in text
    assert "Model D" in text


@pytest.mark.asyncio
async def test_test_explainer_separates_tested_from_requested_size(server):
    async with Client(server) as client:
        text = await render(
            client,
            "tire_test_explainer",
            {"test": "ADAC 2024 summer", "size": "245/35R19 front + 275/35R19 rear"},
        )
    assert "ADAC 2024 summer" in text
    assert "245/35R19 front + 275/35R19 rear" in text
    assert "tires_list_tests" in text and "tires_get_test" in text
    assert "has_mode" in text  # availability annotation, not a tested-size filter
    assert re.search(r"tested size", text, re.IGNORECASE)
    assert re.search(r"separate", text, re.IGNORECASE)
    assert re.search(r"successor", text, re.IGNORECASE)
    assert "test_score" in text
    # concrete caps: bounded list sample, finalist and replacement limits
    assert re.search(r"bounded sample", text, re.IGNORECASE)
    assert re.search(r"≤3 finalists", text)
    assert re.search(r"≤2 replacement lookups", text)
    assert "≤14" in text
    # replacement budget is consistent: the finalist's card is charged to the
    # cross-check step, which must precede the replacement step
    assert text.index("Cross-check") < text.index("Replacements")
    assert "step-4 card" in text
    # staggered input must be split into individual notations before sizes=[...]
    assert re.search(r"individual size notations", text, re.IGNORECASE)
    # organizer/measurements only when evidence supplies them
    assert re.search(r"actually state", text, re.IGNORECASE)


@pytest.mark.asyncio
async def test_test_explainer_without_size_stays_on_tested_size(server):
    async with Client(server) as client:
        text = await render(client, "tire_test_explainer", {"test": "ADAC 2024"})
    assert re.search(r"tested size", text, re.IGNORECASE)
    assert "has_mode" not in text  # no requested size, no availability step


@pytest.mark.asyncio
async def test_model_brief_identity_and_family(server):
    async with Client(server) as client:
        text = await render(
            client,
            "tire_model_brief",
            {"model": "Primacy 4", "brand": "Michelin"},
        )
    assert "Primacy 4" in text and "Michelin" in text
    assert "tires_search" in text and "tires_get_tire" in text
    assert "tires_list_sizes" in text and "tires_get_pros_cons" in text
    assert "tires_list_materials" in text
    assert re.search(r"successor|ancestor", text, re.IGNORECASE)
    assert "null" in text  # no approved reasons ≠ no defects
    assert re.search(r"not stock|not a fitment", text, re.IGNORECASE)
    assert "counters.modes=0" in text  # zero counter never skips verification
    assert "≤9" in text and re.search(r"≤6", text)  # core vs family split
    assert BUDGET.search(text)


@pytest.mark.asyncio
async def test_missing_required_argument_is_rejected(server):
    async with Client(server) as client:
        with pytest.raises(Exception):  # server-side validation error
            await client.get_prompt("tire_model_brief", {})


@pytest.mark.asyncio
async def test_blank_required_argument_is_rejected(server):
    async with Client(server) as client:
        for name, arguments in (
            ("tire_selection_by_size", {"size": "  "}),
            ("tire_test_explainer", {"test": "\t"}),
            ("tire_model_brief", {"model": " "}),
        ):
            with pytest.raises(Exception):
                await client.get_prompt(name, arguments)


@pytest.mark.asyncio
async def test_no_prompt_promises_stock_prices_or_fitment(server):
    async with Client(server) as client:
        for name, arguments in (
            ("tire_selection_by_size", {"size": "205/55R16"}),
            ("tire_comparison", {"model_1": "A", "model_2": "B"}),
            ("tire_test_explainer", {"test": "T"}),
            ("tire_model_brief", {"model": "M"}),
        ):
            text = await render(client, name, arguments)
            assert "untrusted" in text  # remote material text is data
            # the only allowed mention is the explicit disclaimer itself
            assert re.search(r"no stock, price or vehicle-fitment data", text)
