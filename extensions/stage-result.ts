import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const STAGE_FIELDS = {
  intake: {
    task_id: Type.String(),
    objective: Type.String(),
    inputs: Type.Array(Type.Unknown()),
    constraints: Type.Array(Type.Unknown()),
    acceptance_criteria: Type.Array(Type.Unknown()),
    ambiguities: Type.Array(Type.Unknown()),
  },
  plan: {
    summary: Type.String(),
    steps: Type.Array(Type.Unknown()),
    files_expected_to_change: Type.Array(Type.Unknown()),
    risks: Type.Array(Type.Unknown()),
    verification_mapping: Type.Array(Type.Unknown()),
  },
  execute: {
    status: Type.String(),
    summary: Type.String(),
    files_changed: Type.Array(Type.Unknown()),
    commands_run: Type.Array(Type.Unknown()),
    residual_risk: Type.Array(Type.Unknown()),
    completed_steps: Type.Array(Type.Unknown()),
  },
  repair: {
    status: Type.String(),
    diagnosis: Type.String(),
    changes: Type.Array(Type.Unknown()),
    residual_risk: Type.Array(Type.Unknown()),
    completed_steps: Type.Array(Type.Unknown()),
  },
  judge: {
    accepted: Type.Boolean(),
    score: Type.Number(),
    criteria: Type.Array(Type.Unknown()),
    evidence: Type.Array(Type.Unknown()),
    residual_risk: Type.Array(Type.Unknown()),
  },
} as const;

export default function stageResultExtension(pi: ExtensionAPI) {
  const stageName = process.env.HARNESS_STAGE?.startsWith("repair-")
    ? "repair"
    : process.env.HARNESS_STAGE;
  const fields = stageName ? STAGE_FIELDS[stageName as keyof typeof STAGE_FIELDS] : undefined;
  if (!fields) return;

  pi.registerTool({
    name: "submit_stage_result",
    label: "Submit workflow stage result",
    description: "Submit the final typed result for this workflow stage. Call exactly once when the stage is complete.",
    promptSnippet: "When the stage is complete, call submit_stage_result exactly once with every required field. Do not return the result as prose or fenced JSON.",
    promptGuidelines: [
      "Use submit_stage_result only for the final stage result.",
      "Include every required field, using empty arrays when there are no list items.",
      "After the tool succeeds, stop; do not emit a second result.",
    ],
    parameters: Type.Object(fields),
    terminate: true,
    async execute(_toolCallId, params) {
      const details = { ...params, stage: stageName, submitted: true };
      return {
        content: [{ type: "text", text: `Accepted ${stageName} stage result.` }],
        details,
      };
    },
  });
}
