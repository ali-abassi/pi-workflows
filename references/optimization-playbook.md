# Workflow Optimization Sidecar

Build and certify the first harness before optimizing it. Optimization consumes
immutable run and eval artifacts; it never rewrites the active workflow in
place.

## Two suites, two jobs

- **Regression suite:** previously solved cases and safety gates. Target nearly
  100%. Any pass-to-fail change blocks promotion.
- **Capability suite:** hard representative cases with room to improve. Use it
  to measure useful gains and graduate saturated cases into regression.

Use repeated trials where stochasticity matters. Report consistency, not only
best-of-N. Keep frozen held-out cases that the optimizer cannot see.

## Optimization order

1. Fix missing contracts, bad context, weak tools, and flaky verifiers.
2. Improve the prompt or workflow shape against the capability suite.
3. Change model or thinking only with a paired replay.
4. Tune latency, concurrency, caching, and cost without relaxing regression or
   safety gates.

Scaffolding and grounded verifier feedback usually beat blind model swapping.

## LLM judges

Use code for schema, exactness, tool policy, tests, and final-state checks. Use
an LLM judge only for semantic qualities that code cannot discriminate.

A production judge needs:

- 3-6 non-overlapping dimensions with critical gates separated from quality;
- behavioral anchors and an `unknown` or insufficient-evidence outcome;
- only the source material it is allowed to grade;
- structured evidence, per-dimension results, and the gap to the next anchor;
- clean-context execution, versioned model/prompt/rubric hashes, and a human-
  labeled calibration set;
- order swapping for pairwise grading and reporting of disagreement;
- a held-out validation judge or human check distinct from the optimization
  judge.

Never let a generator be its only judge. Never optimize and report against the
same judge.

## Bounded evaluator-optimizer loop

```text
freeze baseline
  -> score candidate on capability + regression + held-out slices
  -> identify one specific high-weight gap
  -> make one change
  -> paired replay
  -> keep only on gain above judge noise with zero regression
  -> stop at target, budget, plateau, or validation divergence
```

Record rejected candidates too. If the optimization score rises while held-out
or human validation stalls, stop and restore the last validated champion.

## Candidate sidecars

- prompt/workflow optimizer;
- model, thinking, cost, and latency router;
- judge calibration and bias dashboard;
- adversarial case miner and capability-to-regression graduation;
- specialized packs for coding, review, research, rating, and browser QA.

Each sidecar proposes a new semantic workflow version and emits paired evidence.
Only the promotion command can change `active.json`.

## Primary sources

- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [OpenAI graders guide](https://developers.openai.com/api/docs/guides/graders)
- [OpenAI prompt optimizer](https://developers.openai.com/api/docs/guides/prompt-optimizer)
- [OpenAI Agents SDK tracing](https://openai.github.io/openai-agents-python/tracing/)
- [SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/)
- [OPRO](https://arxiv.org/abs/2309.03409)
- [DSPy](https://arxiv.org/abs/2310.03714)
- [TextGrad](https://arxiv.org/abs/2406.07496)
- [Judging LLM-as-a-Judge](https://arxiv.org/abs/2306.05685)
