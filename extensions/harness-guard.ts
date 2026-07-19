import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { basename, dirname, isAbsolute, relative, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const auditFile = process.env.HARNESS_AUDIT_FILE;
const trackerFile = process.env.HARNESS_TRACKER_FILE;
const stage = process.env.HARNESS_STAGE || "unknown";
const workdir = canonicalPath(resolve(process.env.HARNESS_WORKDIR || process.cwd()));
const allowedWrites = parsePaths(process.env.HARNESS_ALLOWED_WRITES || "[]");
const immutablePaths = parsePaths(process.env.HARNESS_IMMUTABLE_PATHS || "[]");
const allowBash = process.env.HARNESS_ALLOW_BASH === "1";
const runDir = process.env.HARNESS_RUN_DIR ? resolve(process.env.HARNESS_RUN_DIR) : undefined;
const piBin = process.env.HARNESS_PI_BIN || "pi";
const requiredSteps = parseJson<string[]>(process.env.HARNESS_REQUIRED_STEPS || "[]", []);
const stepValidation = parseJson<Record<string, any>>(process.env.HARNESS_STEP_VALIDATION || "{}", {});
const taskContext = parseJson<Record<string, any>>(process.env.HARNESS_TASK_CONTEXT || "{}", {});
const unsafeBash = /\b(rm|mv|cp|chmod|chown|curl|wget|ssh|scp|git\s+(reset|checkout|clean|push))\b|(^|[^>])>{1,2}[^>]|sed\s+-i|\btee\b/;

function parseJson<T>(raw: string, fallback: T): T {
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function canonicalPath(value: string): string {
  let existing = resolve(value);
  const missing: string[] = [];
  while (!existsSync(existing)) {
    const parent = dirname(existing);
    if (parent === existing) break;
    missing.unshift(basename(existing));
    existing = parent;
  }
  return resolve(realpathSync(existing), ...missing);
}

function parsePaths(raw: string): string[] {
  try {
    const value = JSON.parse(raw);
    return Array.isArray(value) ? value.map((item) => canonicalPath(resolve(workdir, String(item)))) : [];
  } catch {
    return [];
  }
}

function digest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function appendJsonl(file: string | undefined, event: Record<string, unknown>) {
  if (!file) return;
  mkdirSync(dirname(file), { recursive: true });
  appendFileSync(file, JSON.stringify(event) + "\n");
}

function writeJson(file: string, value: unknown) {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(value, null, 2) + "\n");
}

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}



export function strictValidatorVerdict(raw: string, expectedProvider: string | undefined, expectedModel: string): JsonRecord {
  const events: JsonRecord[] = [];
  for (const [offset, line] of raw.split("\n").entries()) {
    if (!line.trim()) continue;
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch {
      throw new Error(`Luna validator emitted malformed Pi JSON at line ${offset + 1}`);
    }
    if (!isRecord(value) || typeof value.type !== "string") {
      throw new Error(`Luna validator emitted an invalid Pi event at line ${offset + 1}`);
    }
    events.push(value);
  }
  if (events.length === 0) throw new Error("Luna validator emitted no Pi events");
  if (events.some((event) => event.type === "extension_error")) {
    throw new Error("Luna validator extension failed");
  }
  if (events.some((event) => event.type === "auto_retry_end" && event.success === false)) {
    throw new Error("Luna validator exhausted an automatic retry");
  }

  let finalAssistantIndex = -1;
  let finalMessage: JsonRecord | undefined;
  for (const [index, event] of events.entries()) {
    if (event.type !== "message_end" || !isRecord(event.message) || event.message.role !== "assistant") continue;
    finalAssistantIndex = index;
    finalMessage = event.message;
  }
  if (!finalMessage) throw new Error("Luna validator returned no assistant message");
  const settledAfterFinal = events.some((event, index) => event.type === "agent_settled" && index > finalAssistantIndex);
  if (!settledAfterFinal) throw new Error("Luna validator never reached agent_settled");
  if (finalMessage.stopReason !== "stop" || typeof finalMessage.errorMessage === "string") {
    throw new Error(`Luna validator ended unsuccessfully: ${String(finalMessage.stopReason)}`);
  }
  if (expectedProvider && finalMessage.provider !== expectedProvider) {
    throw new Error(`Luna validator provider drifted: expected ${expectedProvider}, received ${String(finalMessage.provider)}`);
  }
  if (finalMessage.model !== expectedModel) {
    throw new Error(`Luna validator model drifted: expected ${expectedModel}, received ${String(finalMessage.model)}`);
  }
  if (!Array.isArray(finalMessage.content)) throw new Error("Luna validator returned no assistant JSON");
  const text = finalMessage.content
    .filter((part): part is JsonRecord => isRecord(part) && part.type === "text")
    .map((part) => typeof part.text === "string" ? part.text : "")
    .join("");
  if (!text.trim()) throw new Error("Luna validator returned no assistant JSON");

  let verdict: unknown;
  try {
    verdict = JSON.parse(text);
  } catch {
    throw new Error("Luna validator response was not exact JSON");
  }
  if (
    !isRecord(verdict)
    || typeof verdict.accepted !== "boolean"
    || typeof verdict.score !== "number"
    || !Number.isFinite(verdict.score)
    || verdict.score < 0
    || verdict.score > 10
    || typeof verdict.review !== "string"
    || !verdict.review.trim()
    || !Array.isArray(verdict.guidance)
    || !verdict.guidance.every((item) => typeof item === "string")
    || !["strong", "adequate", "weak"].includes(String(verdict.evidence_quality))
    || typeof verdict.confidence !== "number"
    || !Number.isFinite(verdict.confidence)
    || verdict.confidence < 0
    || verdict.confidence > 1
  ) {
    throw new Error("Luna validator response failed its verdict schema");
  }
  return verdict;
}

function splitModel(value: string): [string | undefined, string] {
  const slash = value.indexOf("/");
  return slash < 0 ? [undefined, value] : [value.slice(0, slash), value.slice(slash + 1)];
}

function collectArtifactEvidence(paths: string[] | undefined): Array<Record<string, unknown>> {
  const evidence: Array<Record<string, unknown>> = [];
  let remainingBytes = 32_000;
  for (const raw of (paths || []).slice(0, 6)) {
    const candidate = canonicalPath(resolve(workdir, raw.replace(/^@/, "")));
    if (!isWithin(candidate, workdir)) {
      evidence.push({ path: raw, error: "outside_workdir" });
      continue;
    }
    if (!existsSync(candidate) || !statSync(candidate).isFile()) {
      evidence.push({ path: raw, error: "missing_or_not_file" });
      continue;
    }
    const data = readFileSync(candidate);
    const take = Math.max(0, Math.min(data.length, 12_000, remainingBytes));
    remainingBytes -= take;
    evidence.push({
      path: relative(workdir, candidate),
      size: data.length,
      sha256: createHash("sha256").update(data).digest("hex"),
      excerpt: data.subarray(0, take).toString("utf8"),
      truncated: take < data.length,
    });
    if (remainingBytes <= 0) break;
  }
  return evidence;
}

function audit(event: Record<string, unknown>) {
  const record = { timestamp: new Date().toISOString(), source: "pi_hook", stage, ...event };
  appendJsonl(auditFile, record);
  appendJsonl(trackerFile, record);
}

function candidatePath(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length === 0) return undefined;
  return canonicalPath(resolve(workdir, value));
}

function isWithin(candidate: string, root: string): boolean {
  const rel = relative(root, candidate);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

function writeAllowed(candidate: string): boolean {
  return allowedWrites.some((root) => isWithin(candidate, root));
}

function isImmutable(candidate: string): boolean {
  return immutablePaths.some((root) => isWithin(candidate, root));
}

export default function (pi: ExtensionAPI) {
  let nextStep = 1;
  const attempts = new Map<number, number>();
  function persistedAttemptCount(index: number): number {
    if (!runDir) return 0;
    const attemptDir = resolve(runDir, "step-validation", String(index).padStart(2, "0"));
    if (!existsSync(attemptDir) || !statSync(attemptDir).isDirectory()) return 0;
    return readdirSync(attemptDir).reduce((maximum, name) => {
      const match = /^attempt-(\d+)\.json$/.exec(name);
      return match ? Math.max(maximum, Number(match[1])) : maximum;
    }, 0);
  }

  if (runDir) {
    for (let index = 1; index <= requiredSteps.length; index += 1) {
      const count = persistedAttemptCount(index);
      if (count > 0) attempts.set(index, count);
      const accepted = resolve(runDir, "step-validation", String(index).padStart(2, "0"), "accepted.json");
      if (existsSync(accepted)) nextStep = index + 1;
      else break;
    }
  }


  async function validateReportedStep(
    params: { index: number; name: string; summary: string; evidence: string; artifacts?: string[] },
    signal: AbortSignal | undefined,
    onUpdate: ((result: any) => void) | undefined,
  ) {
    const expectedName = requiredSteps[params.index - 1];
    if (!runDir || !stepValidation.enabled) {
      return { accepted: false, score: 0, review: "Step validation is not configured for this run.", guidance: ["Do not call harness_step."] };
    }
    if (!Number.isInteger(params.index) || params.index !== nextStep || params.name !== expectedName) {
      const result = {
        accepted: false,
        score: 0,
        review: `Expected step ${nextStep}: ${requiredSteps[nextStep - 1] || "none"}`,
        guidance: ["Report required steps exactly once and in order."],
      };
      audit({ type: "step_validation", stepIndex: params.index, stepName: params.name, accepted: false, reason: "order_or_name_mismatch" });
      return result;
    }
    const attempt = (attempts.get(params.index) || 0) + 1;
    attempts.set(params.index, attempt);
    const maximum = Number(stepValidation.max_attempts_per_step || 2);
    if (attempt > maximum) {
      return { accepted: false, score: 0, review: "Maximum validation attempts exhausted.", guidance: ["Stop and let the harness report failure."] };
    }

    onUpdate?.({ content: [{ type: "text", text: `Luna is reviewing step ${params.index}/${requiredSteps.length}…` }], details: { step: params.index, phase: "validating" } });
    const artifactEvidence = collectArtifactEvidence(params.artifacts);
    const [provider, model] = splitModel(String(stepValidation.model || "openai-codex/gpt-5.6-luna"));
    const prompt = [
      "Return only JSON. Independently review one completed deterministic-workflow step.",
      "Required keys: accepted (boolean), score (0-10 number), review (string), guidance (array of strings), evidence_quality (one of strong, adequate, weak), confidence (0-1 number).",
      "Accept only when the evidence directly supports the named step and the work is consistent with the task constraints. Be concise.",
      `TASK CONTEXT:\n${JSON.stringify(taskContext, null, 2)}`,
      `STEP:\n${JSON.stringify(params, null, 2)}`,
      `BOUNDED ARTIFACT EVIDENCE:\n${JSON.stringify(artifactEvidence, null, 2)}`,
    ].join("\n\n");
    const args = [
      "--mode", "json", "--no-session", "--no-approve", "--offline", "--no-extensions", "--no-skills",
      "--no-context-files", "--no-prompt-templates", "--no-themes", "--system-prompt",
      "You are a bounded step verifier. Return only the requested JSON decision from supplied evidence.",
    ];
    if (provider) args.push("--provider", provider);
    args.push("--model", model, "--thinking", String(stepValidation.thinking || "low"), "--no-tools", prompt);

    let validator: JsonRecord;
    let validatorError: string | undefined;
    try {
      const execution = await pi.exec(piBin, args, { signal, timeout: Number(stepValidation.timeout_seconds || 120) * 1000 });
      if (execution.code !== 0) throw new Error(`validator exited ${execution.code}: ${execution.stderr}`);
      validator = strictValidatorVerdict(execution.stdout, provider, model);
    } catch (error) {
      validatorError = error instanceof Error ? error.message : String(error);
      validator = { accepted: false, score: 0, review: "Validator failed to produce a decision.", guidance: [validatorError], evidence_quality: "weak", confidence: 0 };
    }
    const threshold = Number(stepValidation.min_score || 8);
    const validatorAccepted = validator.accepted === true && Number(validator.score) >= threshold && !validatorError;
    const mode = stepValidation.mode === "advisory" ? "advisory" : "gate";
    const acceptedForProgress = validatorAccepted || mode === "advisory";
    const record = {
      step_index: params.index,
      step_name: params.name,
      attempt,
      mode,
      threshold,
      validator_model: stepValidation.model || "openai-codex/gpt-5.6-luna",
      validator_accepted: validatorAccepted,
      accepted_for_progress: acceptedForProgress,
      validator_error: validatorError,
      claim: params,
      artifact_evidence: artifactEvidence,
      review: validator,
      timestamp: new Date().toISOString(),
    };
    const attemptDir = resolve(runDir, "step-validation", String(params.index).padStart(2, "0"));
    writeJson(resolve(attemptDir, `attempt-${String(attempt).padStart(2, "0")}.json`), record);
    if (acceptedForProgress) {
      writeJson(resolve(attemptDir, "accepted.json"), record);
      writeJson(resolve(runDir, "stages", "task-steps", `${String(params.index).padStart(2, "0")}.json`), {
        index: params.index,
        name: params.name,
        status: validatorAccepted ? "reviewed" : "claimed",
        evidence: params.evidence,
        mechanically_verified: false,
        model_validation: { accepted: validatorAccepted, score: validator.score, review: validator.review, guidance: validator.guidance, attempt, model_label: "Luna" },
      });
      nextStep += 1;
    }
    audit({
      type: "step_validation",
      stepIndex: params.index,
      stepName: params.name,
      attempt,
      accepted: validatorAccepted,
      acceptedForProgress,
      mode,
      score: validator.score,
      review: validator.review,
      guidance: validator.guidance,
    });
    onUpdate?.({ content: [{ type: "text", text: `${validatorAccepted ? "Accepted" : "Needs work"}: ${validator.review}` }], details: record });
    return {
      accepted: validatorAccepted,
      accepted_for_progress: acceptedForProgress,
      score: validator.score,
      review: validator.review,
      guidance: validator.guidance,
      evidence_quality: validator.evidence_quality,
      attempts_remaining: Math.max(0, maximum - attempt),
    };
  }

  let stepQueue: Promise<any> = Promise.resolve();
  pi.registerTool({
    name: "harness_step",
    label: "Validate workflow step",
    description: "Report one required workflow step in exact order and receive an independent Luna review before continuing.",
    promptSnippet: "Report and validate completion of the next required workflow step",
    promptGuidelines: [
      "When harness_step is active, call harness_step after completing each required step, exactly once and in order.",
      "If harness_step rejects a step, use its guidance to repair the work and call harness_step again for the same step before continuing.",
    ],
    parameters: Type.Object({
      index: Type.Integer({ minimum: 1 }),
      name: Type.String(),
      summary: Type.String(),
      evidence: Type.String(),
      artifacts: Type.Optional(Type.Array(Type.String())),
    }),
    async execute(_toolCallId, params, signal, onUpdate) {
      const resultPromise = stepQueue.then(() => validateReportedStep(params, signal, onUpdate));
      stepQueue = resultPromise.then(() => undefined, () => undefined);
      const result = await resultPromise;
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
    },
  });

  for (const name of ["session_start", "agent_start", "turn_start", "turn_end", "agent_end", "agent_settled", "session_shutdown", "model_select"] as const) {
    pi.on(name, async (event, ctx) => {
      audit({ type: name, eventDigest: digest(event) });
      if ((ctx as any).hasUI) {
        (ctx as any).ui.setStatus("harness", `harness ${stage}: ${name}`);
      }
    });
  }

  for (const name of ["tool_execution_start", "tool_execution_update", "tool_execution_end"] as const) {
    pi.on(name, async (event, ctx) => {
      audit({
        type: name,
        toolCallId: (event as any).toolCallId,
        toolName: (event as any).toolName,
        isError: (event as any).isError,
        eventDigest: digest(event),
      });
      if ((ctx as any).hasUI) {
        (ctx as any).ui.setStatus("harness", `harness ${stage}: ${(event as any).toolName || name}`);
      }
    });
  }

  pi.on("tool_call", async (event, ctx) => {
    const input = event.input as Record<string, unknown>;
    const path = candidatePath(input.path);
    const command = typeof input.command === "string" ? input.command : undefined;
    let blocked = false;
    let reason: string | undefined;

    if (event.toolName === "bash" && !allowBash) {
      blocked = true;
      reason = "Harness policy blocks model-controlled bash; declare supervisor execution_commands instead";
    }

    if (event.toolName === "bash" && allowBash && command && unsafeBash.test(command)) {
      blocked = true;
      reason = "Harness policy blocks bash commands with broad mutation, network, git, or redirection risk";
    }

    if ((event.toolName === "write" || event.toolName === "edit") && path) {
      if (isImmutable(path)) {
        blocked = true;
        reason = `Immutable path blocked: ${relative(workdir, path)}`;
      } else if (!writeAllowed(path)) {
        blocked = true;
        reason = `Write path is outside the approved allowlist: ${relative(workdir, path)}`;
      }
    }

    audit({
      type: "tool_call",
      toolCallId: event.toolCallId,
      toolName: event.toolName,
      path: path ? relative(workdir, path) : undefined,
      commandPreview: command ? command.slice(0, 240) : undefined,
      inputDigest: digest(input),
      blocked,
      reason,
    });
    if ((ctx as any).hasUI) {
      const marker = blocked ? "blocked" : "allowed";
      (ctx as any).ui.setStatus("harness", `harness ${stage}: ${marker} ${event.toolName}`);
      (ctx as any).ui.setWidget("harness-tracker", [
        `stage: ${stage}`,
        `tool: ${event.toolName}`,
        `decision: ${marker}`,
        reason ? `reason: ${reason}` : "reason: allowed by harness policy",
      ]);
    }
    return blocked ? { block: true, reason } : undefined;
  });

  pi.on("tool_result", async (event) => {
    audit({
      type: "tool_result",
      toolCallId: event.toolCallId,
      toolName: event.toolName,
      isError: event.isError,
      resultDigest: digest({ content: event.content, details: event.details }),
    });
  });
}
