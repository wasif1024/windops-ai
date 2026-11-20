import { type ReactNode, useState } from "react";
import { Button } from "../../../components/Button";
import { INPUT, LABEL } from "../../../components/Panel";
import { titleCase } from "../../fleet/format";
import { ACTIONS, type Action } from "../api";
import { EMPTY_PART, EMPTY_TASK, type PartForm, type PlanForm, type TaskForm } from "../form";

function Field({ id, label, children, hint }: { id: string; label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="space-y-1">
      <label className={LABEL} htmlFor={id}>
        {label}
      </label>
      {children}
      {hint && <p className="text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

function replace<T>(items: T[], index: number, changes: Partial<T>): T[] {
  return items.map((item, at) => (at === index ? { ...item, ...changes } : item));
}

/**
 * A plan's fields: what to do, its tasks, parts, procedures, window and downtime. The Maintenance
 * Service validates it as it is saved, so this only asks for each field and leaves the rules there.
 */
export function PlanEditor({
  initial,
  busy,
  submitLabel,
  onSave,
  onCancel,
  before,
}: {
  initial: PlanForm;
  busy: boolean;
  submitLabel: string;
  onSave: (form: PlanForm) => void;
  onCancel?: () => void;
  /** Shown first, such as the case a new draft is for. */
  before?: ReactNode;
}) {
  const [form, setForm] = useState(initial);
  const set = (changes: Partial<PlanForm>) => setForm((current) => ({ ...current, ...changes }));
  const setTask = (index: number, changes: Partial<TaskForm>) => set({ tasks: replace(form.tasks, index, changes) });
  const setPart = (index: number, changes: Partial<PartForm>) => set({ parts: replace(form.parts, index, changes) });
  return (
    <form
      className="space-y-5"
      aria-label="Plan"
      onSubmit={(event) => {
        event.preventDefault();
        onSave(form);
      }}
    >
      {before}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field id="plan-title" label="Title">
          <input id="plan-title" className={INPUT} value={form.title} maxLength={200} onChange={(e) => set({ title: e.target.value })} />
        </Field>
        <Field id="plan-action" label="Action">
          <select id="plan-action" className={INPUT} value={form.action} onChange={(e) => set({ action: e.target.value as Action })}>
            {ACTIONS.map((action) => (
              <option key={action} value={action}>
                {titleCase(action)}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <Field id="plan-summary" label="Summary">
        <textarea id="plan-summary" className={INPUT} rows={3} value={form.summary} maxLength={4000} onChange={(e) => set({ summary: e.target.value })} />
      </Field>

      <fieldset className="space-y-3">
        <legend className={LABEL}>Tasks</legend>
        {form.tasks.map((task, index) => (
          <div key={index} className="space-y-2 rounded-md border border-slate-200 p-3" data-testid={`task-${index}`}>
            <Field id={`task-${index}-description`} label={`Task ${index + 1}`}>
              <input
                id={`task-${index}-description`}
                className={INPUT}
                value={task.description}
                maxLength={2000}
                onChange={(e) => setTask(index, { description: e.target.value })}
              />
            </Field>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
              <Field id={`task-${index}-hours`} label="Hours">
                <input
                  id={`task-${index}-hours`}
                  className={INPUT}
                  type="number"
                  min="0"
                  max="240"
                  step="0.5"
                  inputMode="decimal"
                  value={task.duration_hours}
                  onChange={(e) => setTask(index, { duration_hours: e.target.value })}
                />
              </Field>
              <Field id={`task-${index}-crew`} label="Crew">
                <input
                  id={`task-${index}-crew`}
                  className={INPUT}
                  type="number"
                  min="1"
                  max="20"
                  inputMode="numeric"
                  value={task.crew_size}
                  onChange={(e) => setTask(index, { crew_size: e.target.value })}
                />
              </Field>
              <label className="col-span-2 flex items-center gap-2 text-sm text-slate-700 sm:col-span-1 sm:pb-2">
                <input type="checkbox" checked={task.requires_climb} onChange={(e) => setTask(index, { requires_climb: e.target.checked })} />
                Needs a climb
              </label>
            </div>
            {form.tasks.length > 1 && (
              <button
                type="button"
                className="text-sm text-red-700 hover:underline"
                onClick={() => set({ tasks: form.tasks.filter((_, at) => at !== index) })}
              >
                Remove task {index + 1}
              </button>
            )}
          </div>
        ))}
        <Button variant="outline" className="text-brand-primary" onClick={() => set({ tasks: [...form.tasks, { ...EMPTY_TASK }] })}>
          Add a task
        </Button>
      </fieldset>

      <fieldset className="space-y-3">
        <legend className={LABEL}>Parts</legend>
        {form.parts.length === 0 && <p className="text-sm text-slate-500">No parts. Find them in Parts lookup.</p>}
        {form.parts.map((part, index) => (
          <div key={index} className="grid grid-cols-[1fr_5rem_auto] items-end gap-2" data-testid={`part-${index}`}>
            <Field id={`part-${index}-number`} label="Part number">
              <input
                id={`part-${index}-number`}
                className={INPUT}
                value={part.part_number}
                maxLength={64}
                onChange={(e) => setPart(index, { part_number: e.target.value })}
              />
            </Field>
            <Field id={`part-${index}-quantity`} label="Quantity">
              <input
                id={`part-${index}-quantity`}
                className={INPUT}
                type="number"
                min="1"
                max="1000"
                inputMode="numeric"
                value={part.quantity}
                onChange={(e) => setPart(index, { quantity: e.target.value })}
              />
            </Field>
            <button
              type="button"
              aria-label={`Remove part ${index + 1}`}
              className="pb-2 text-sm text-red-700 hover:underline"
              onClick={() => set({ parts: form.parts.filter((_, at) => at !== index) })}
            >
              Remove
            </button>
          </div>
        ))}
        <Button variant="outline" className="text-brand-primary" onClick={() => set({ parts: [...form.parts, { ...EMPTY_PART }] })}>
          Add a part
        </Button>
      </fieldset>

      <Field id="plan-procedures" label="Procedures" hint="One knowledge citation a line, such as kn:GBX-OIL-01@3#12.">
        <textarea
          id="plan-procedures"
          className={`${INPUT} font-mono`}
          rows={2}
          value={form.procedures}
          onChange={(e) => set({ procedures: e.target.value })}
        />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field id="plan-start" label="Starts">
          <input id="plan-start" className={INPUT} type="datetime-local" value={form.planned_start} onChange={(e) => set({ planned_start: e.target.value })} />
        </Field>
        <Field id="plan-end" label="Ends">
          <input id="plan-end" className={INPUT} type="datetime-local" value={form.planned_end} onChange={(e) => set({ planned_end: e.target.value })} />
        </Field>
        <Field id="plan-downtime" label="Expected downtime, hours">
          <input
            id="plan-downtime"
            className={INPUT}
            type="number"
            min="0"
            max="720"
            step="0.5"
            inputMode="decimal"
            value={form.estimated_downtime_hours}
            onChange={(e) => set({ estimated_downtime_hours: e.target.value })}
          />
        </Field>
        <Field id="plan-impact" label="Production impact estimate, if any" hint="Its result ID, such as imp_…">
          <input id="plan-impact" className={INPUT} value={form.impact_result_id} maxLength={64} onChange={(e) => set({ impact_result_id: e.target.value })} />
        </Field>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={busy}>
          {submitLabel}
        </Button>
        {onCancel && (
          <Button variant="outline" className="text-slate-700" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  );
}
