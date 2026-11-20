// The plan form's state, as the planner types it, and the plan it becomes. Pure, so it is tested
// on its own. The Maintenance Service checks the plan in full: this only shapes it.
import type { Action, Plan, PlanIn } from "./api";

export interface TaskForm {
  description: string;
  duration_hours: string;
  crew_size: string;
  requires_climb: boolean;
}

export interface PartForm {
  part_number: string;
  quantity: string;
}

export interface PlanForm {
  title: string;
  action: Action;
  summary: string;
  tasks: TaskForm[];
  parts: PartForm[];
  /** One citation a line, such as kn:GBX-OIL-01@3#12. */
  procedures: string;
  /** As a datetime-local input holds it: the planner's own time, without a zone. */
  planned_start: string;
  planned_end: string;
  estimated_downtime_hours: string;
  impact_result_id: string;
}

export const EMPTY_TASK: TaskForm = { description: "", duration_hours: "", crew_size: "1", requires_climb: false };
export const EMPTY_PART: PartForm = { part_number: "", quantity: "1" };

export function emptyPlan(): PlanForm {
  return {
    title: "",
    action: "inspection",
    summary: "",
    tasks: [{ ...EMPTY_TASK }],
    parts: [],
    procedures: "",
    planned_start: "",
    planned_end: "",
    estimated_downtime_hours: "0",
    impact_result_id: "",
  };
}

/** An ISO time as a datetime-local input shows it, in the browser's own zone. */
export function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const when = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}T${pad(when.getHours())}:${pad(when.getMinutes())}`;
}

/** A datetime-local value as ISO 8601 in UTC, or null when empty or not a time. */
export function fromLocalInput(value: string): string | null {
  if (!value) return null;
  const when = new Date(value);
  return Number.isNaN(when.getTime()) ? null : when.toISOString();
}

function number(text: string): number {
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** The form as a plan for the Web BFF. Empty part rows are left out. */
export function toPlan(form: PlanForm): PlanIn {
  return {
    title: form.title.trim(),
    action: form.action,
    summary: form.summary.trim(),
    tasks: form.tasks.map((task) => ({
      description: task.description.trim(),
      duration_hours: number(task.duration_hours),
      crew_size: Math.round(number(task.crew_size)),
      requires_climb: task.requires_climb,
    })),
    parts: form.parts
      .filter((part) => part.part_number.trim())
      .map((part) => ({ part_number: part.part_number.trim(), quantity: Math.round(number(part.quantity)) })),
    procedures: form.procedures
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean),
    planned_start: fromLocalInput(form.planned_start),
    planned_end: fromLocalInput(form.planned_end),
    estimated_downtime_hours: number(form.estimated_downtime_hours),
    impact_result_id: form.impact_result_id.trim(),
  };
}

/** A kept plan as the form shows it, to edit a draft. */
export function fromPlan(plan: Plan): PlanForm {
  return {
    title: plan.title,
    action: plan.action,
    summary: plan.summary,
    tasks: plan.tasks.map((task) => ({
      description: task.description,
      duration_hours: String(task.duration_hours),
      crew_size: String(task.crew_size),
      requires_climb: task.requires_climb,
    })),
    parts: plan.parts.map((part) => ({ part_number: part.part_number, quantity: String(part.quantity) })),
    procedures: plan.procedures.join("\n"),
    planned_start: toLocalInput(plan.planned_start),
    planned_end: toLocalInput(plan.planned_end),
    estimated_downtime_hours: String(plan.estimated_downtime_hours),
    impact_result_id: plan.impact_result_id,
  };
}
