from __future__ import annotations

import dspy
import time
import logging
import functools
from .memory import load_context, append_entry

logger = logging.getLogger(__name__)

# ---- Retry decorator ----


def retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0):
    """Decorator for retrying LLM calls with exponential backoff"""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    if attempt == max_retries:
                        logger.error(f"Final attempt failed for {func.__name__}: {e}")
                        raise

                    delay = base_delay * (2**attempt)
                    logger.warning(
                        f"Attempt {attempt + 1} failed for {func.__name__}: {e}. Retrying in {delay}s..."
                    )
                    time.sleep(delay)

            # Should never reach here, but just in case
            raise last_exception

        return wrapper

    return decorator


# ---- Improved Signatures ----


class HighOrbitAdvice(dspy.Signature):
    """You are a brutally honest startup advisor in the YC tradition.

    Give specific, actionable advice that optimizes for $1B vs $0 outcomes.
    Be concrete, direct, and tailored to the user's context (persona, stage,
    constraints). Treat the playbook as heuristics, not as text to repeat.
    Do not regurgitate playbook lines; instead, synthesize novel guidance and
    tie it to the user's scenario with clear reasoning.

    Output format requirements:
    - Advice: 2-3 paragraphs of specific, actionable guidance (no bullets)
    - Actions: List exactly 3-5 specific tasks, one per line, no formatting
    - Metric: One clear metric to track progress
    - Risks: List exactly 3 risks, one per line, no formatting
    """

    context: str = dspy.InputField(desc="Founder/company background and context")
    history: str = dspy.InputField(desc="Previous conversation messages")
    playbook: str = dspy.InputField(desc="Advisory heuristics and frameworks")
    tool_results: str = dspy.InputField(
        desc="Results from any financial/analytical tools", default=""
    )

    advice: str = dspy.OutputField(
        desc="2-3 paragraphs of specific, actionable advice tailored to their situation"
    )
    actions_48h: str = dspy.OutputField(
        desc="List 3-5 specific tasks to complete in 48 hours, one per line, no bullet points or numbers"
    )
    metric_to_watch: str = dspy.OutputField(
        desc="One metric that best predicts progress in 1-2 weeks"
    )
    risks: str = dspy.OutputField(
        desc="List 3 immediate risks or assumptions to test, one per line, no bullet points or numbers"
    )


class BrutalCritique(dspy.Signature):
    """Score advice on specificity, actionability, courage, and $1B-orbit alignment.

    Look for:
    - Specificity: Are the recommendations concrete and measurable?
    - Actionability: Can the founder actually do this in 48h?
    - Courage: Does this push them toward big outcomes vs playing it safe?
    - Relevance: Is this adapted to their specific situation vs generic advice?

    Be harsh but constructive. Score 0-10.
    """

    advice: str = dspy.InputField()
    context: str = dspy.InputField(desc="Original context to judge relevance")

    feedback: str = dspy.OutputField(desc="Terse, constructive criticism")
    score: int = dspy.OutputField(desc="Score 0-10")


# ---- Enhanced Modules ----


class ToolWrapper:
    """Wrapper to make functions compatible with DSPy tools"""

    def __init__(self, func, name, description):
        self.func = func
        self.name = name
        self.description = description

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)


class HighOrbitAdvisor(dspy.Module):
    def __init__(self):
        super().__init__()

        # For now, let's simplify and not use ReAct until we can test it properly
        # self.generate = ReAct(HighOrbitAdvice, tools=tools, max_iters=3)
        self.generate = dspy.Predict(HighOrbitAdvice)
        self.critic = dspy.Predict(BrutalCritique)

    @retry_with_backoff(max_retries=2, base_delay=1.0)
    def _call_llm_with_retry(self, module, **kwargs):
        """Call LLM with retry logic and basic latency + token/cost logging"""
        start = time.time()
        try:
            result = module(**kwargs)
            return result
        finally:
            duration_ms = (time.time() - start) * 1000.0
            try:
                from .config import get_config

                cfg = get_config()
                hist = kwargs.get("history", "") or ""
                ctx = kwargs.get("context", "") or ""
                play = kwargs.get("playbook", "") or ""
                tool = kwargs.get("tool_results", "") or ""

                in_chars = len(hist) + len(ctx) + len(play) + len(tool)
                out_chars = 0
                if "result" in locals() and hasattr(result, "__dict__"):
                    for f in [
                        "advice",
                        "actions_48h",
                        "metric_to_watch",
                        "risks",
                        "feedback",
                    ]:
                        val = getattr(result, f, None)
                        if isinstance(val, str):
                            out_chars += len(val)

                est_in_toks = int((in_chars / 4.0) + 0.5)
                est_out_toks = int((out_chars / 4.0) + 0.5)
                if cfg.track_usage and (
                    cfg.cost_per_1k_prompt > 0 or cfg.cost_per_1k_completion > 0
                ):
                    cost = (est_in_toks / 1000.0) * cfg.cost_per_1k_prompt + (
                        est_out_toks / 1000.0
                    ) * cfg.cost_per_1k_completion
                    logger.info(
                        f"LLM {module.__class__.__name__}: {duration_ms:.0f} ms, in≈{est_in_toks} tok, out≈{est_out_toks} tok, cost≈${cost:.4f}"
                    )
                else:
                    logger.info(
                        f"LLM {module.__class__.__name__}: {duration_ms:.0f} ms, in≈{est_in_toks} tok, out≈{est_out_toks} tok"
                    )
            except Exception:
                logger.debug("Could not log LLM call metrics")

    def _clean_output(self, text: str) -> str:
        """Clean up LLM output by removing metadata and formatting markers"""
        if not text:
            return text

        # Convert to string if it's not already
        text = str(text)

        # If this contains metadata markers, this field got corrupted with other content
        if any(
            marker in text
            for marker in [
                "Context:",
                "History:",
                "Playbook:",
                "Tool Results:",
                "Advice:",
                "Actions:",
                "Metric:",
                "Risks:",
            ]
        ):
            # Extract the first meaningful content line that doesn't look like metadata
            lines = text.split("\n")
            for line in lines:
                line = line.strip().replace("**", "")
                # Skip metadata headers and empty lines
                if (
                    not line
                    or ":" in line[:20]
                    or line.startswith("**")
                    or "Context" in line
                    or "History" in line
                ):
                    continue
                # Return first good content line
                if len(line) > 10:
                    return line

            # If no good line found, return first non-empty line
            for line in lines:
                line = line.strip().replace("**", "")
                if line and len(line) > 5:
                    return line

        # Otherwise do normal cleaning for multi-line content
        lines = text.split("\n")
        cleaned_lines = []

        for line in lines:
            line = line.strip()
            # Skip empty lines and metadata markers
            if not line or (line.startswith("**") and line.endswith("**")):
                continue
            # Skip lines that look like field labels
            if line.endswith(":") and len(line.split()) <= 3:
                continue
            # Remove bold formatting
            line = line.replace("**", "").strip()
            if line:
                cleaned_lines.append(line)

        return "\n".join(cleaned_lines)

    def forward(
        self, history: list[dict[str, str]], playbook: str, context: str | None = None
    ):
        try:
            # Load context and format inputs
            context = context or load_context()
            history_str = "\n".join([f"{m['role']}: {m['content']}" for m in history])

            # Get recent advice for better continuity
            from .memory import get_recent_advice

            recent = get_recent_advice(limit=3)
            recent_context = ""
            if recent:
                recent_context = "\n\nRecent advice patterns:\n" + "\n".join(
                    [f"- {r['metric_to_watch']}" for r in recent[:2]]
                )

            context_with_history = context + recent_context

            # Attempt tool-augmented analysis for structured snippets
            tool_results = ""
            try:
                import re

                from .tools.retention import calculate_cohort_retention
                from .tools.funnel import analyze_funnel, FunnelStep
                from .tools.finance import runway_months, expected_value

                # Extract inline JSON-ish snippet if present
                m = re.search(r"(\[.*\]|\{.*\})", history_str, re.DOTALL)
                if m:
                    snippet = m.group(1)
                    import json

                    data = json.loads(snippet)
                    if isinstance(data, list) and data and isinstance(data[0], list):
                        # Retention cohorts
                        res = calculate_cohort_retention(data)
                        tool_results = (
                            f"Cohorts={len(res)}; Example retention for first cohort: "
                            f"{', '.join(f'{r:.1f}%' for r in res[0].retention_rates[:4])}"
                        )
                    elif isinstance(data, list) and data and isinstance(data[0], dict):
                        # Funnel steps
                        steps = [FunnelStep(**s) for s in data]
                        res = analyze_funnel(steps)
                        rates = ", ".join(f"{r:.1f}%" for r in res.conversion_rates)
                        tool_results = f"Funnel steps={len(steps)}; Conversion: {rates}"
                    elif isinstance(data, dict) and {"cash", "burn"} <= set(
                        data.keys()
                    ):
                        rr = runway_months(
                            float(data["cash"]),
                            float(data["burn"]),
                            float(data.get("growth", 0)),
                        )
                        tool_results = f"Runway≈{rr.months:.1f}m; Alive={'Yes' if rr.default_alive else 'No'}"
                    elif isinstance(data, dict) and {
                        "p_upside",
                        "ev_upside",
                        "p_mid",
                        "ev_mid",
                        "p_down",
                        "ev_down",
                    } <= set(data.keys()):
                        ev = expected_value(
                            float(data["p_upside"]),
                            float(data["ev_upside"]),
                            float(data["p_mid"]),
                            float(data["ev_mid"]),
                            float(data["p_down"]),
                            float(data["ev_down"]),
                        )
                        tool_results = f"EV≈${ev:,.0f} across scenarios"
            except Exception:
                tool_results = tool_results or ""

            # Best-of-N generation and rerank by critic
            from .config import get_config

            cfg = get_config()
            best_of_n = max(1, int(getattr(cfg, "best_of_n", 1)))
            overlap_alpha = float(getattr(cfg, "overlap_alpha", 2.0) or 2.0)
            best_payload = None
            best_score = -1e9

            # Optional separate critic LM
            critic_lm = None
            try:
                import dspy as _dspy

                if getattr(cfg, "critic_model", None):
                    critic_lm = _dspy.LM(model=cfg.critic_model)
            except Exception:
                critic_lm = None

            def _ngram_set(text: str, n: int = 3) -> set[str]:
                tokens = [t for t in text.lower().split() if t]
                return set(
                    [
                        " ".join(tokens[i : i + n])
                        for i in range(0, max(0, len(tokens) - n + 1))
                    ]
                )

            def _overlap_ratio(a: str, b: str, n: int = 3) -> float:
                if not a or not b:
                    return 0.0
                A = _ngram_set(a, n)
                B = _ngram_set(b, n)
                if not A or not B:
                    return 0.0
                inter = len(A & B)
                union = len(A | B)
                return inter / union

            for _ in range(best_of_n):
                logger.info("Generating advice with LLM")
                draft = self._call_llm_with_retry(
                    self.generate,
                    history=history_str,
                    playbook=playbook,
                    context=context_with_history,
                    tool_results=tool_results or "No tools used in this session",
                )

                # Critique with retry (optionally on separate critic LM)
                logger.info("Getting critique")
                advice_clean = self._clean_output(draft.advice)
                kwargs = dict(advice=advice_clean, context=context_with_history)
                if critic_lm is not None:
                    critique = self._call_llm_with_retry(
                        self.critic, **kwargs, lm=critic_lm
                    )
                else:
                    critique = self._call_llm_with_retry(self.critic, **kwargs)

                score_raw = int(getattr(critique, "score", 0) or 0)
                overlap = _overlap_ratio(advice_clean, playbook)
                score = score_raw - overlap_alpha * overlap
                if score > best_score:
                    best_score = score
                    best_payload = (
                        advice_clean,
                        self._clean_output(draft.actions_48h),
                        self._clean_output(draft.metric_to_watch),
                        self._clean_output(draft.risks),
                        critique.feedback,
                        score_raw,
                    )

            advice, actions_48h, metric_to_watch, risks, feedback, score = best_payload
            result = dspy.Prediction(
                advice=advice,
                actions_48h=actions_48h,
                metric_to_watch=metric_to_watch,
                risks=risks,
                critique=feedback,
                score=score,
            )

            logger.info(f"Advice generated successfully, score: {score}")
            return result

        except Exception as e:
            logger.error(f"Failed to generate advice: {e}")
            # Return a fallback response with proper string format
            return dspy.Prediction(
                advice="I'm experiencing technical difficulties with the AI service. Please try again in a moment or check if your API key is valid.",
                actions_48h="Try again in 5 minutes\nCheck your internet connection\nVerify API key is valid",
                metric_to_watch="System availability",
                risks="Technical issues\nService interruption\nAPI key problems",
                critique="System error - could not generate proper advice",
                score=0,
            )


def log_advice(history: list[dict[str, str]], result: dspy.Prediction) -> None:
    """Log advice to persistent storage"""
    try:
        # Parse actions and risks from string format for storage
        actions_list = []
        if isinstance(result.actions_48h, str):
            actions_list = [
                line.strip().lstrip("123456789. -•")
                for line in result.actions_48h.split("\n")
                if line.strip() and not line.strip().startswith("**")
            ]
        else:
            actions_list = result.actions_48h

        risks_list = []
        if isinstance(result.risks, str):
            risks_list = [
                line.strip().lstrip("123456789. -•")
                for line in result.risks.split("\n")
                if line.strip() and not line.strip().startswith("**")
            ]
        else:
            risks_list = result.risks

        append_entry(
            "advice",
            {
                "history": history,
                "advice": result.advice,
                "actions_48h": actions_list,
                "metric_to_watch": result.metric_to_watch,
                "risks": risks_list,
                "score": result.score,
                "critique": result.critique,
            },
        )
    except Exception as e:
        logger.error(f"Failed to log advice: {e}")
