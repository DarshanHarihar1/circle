"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

export type CravingCardData = {
  veg: string;
  budget_max: number | null;
  cuisine_vibe: string | null;
  must_have: string | null;
  allergies: string[];
  deal_breakers: string[];
};

const DIET = [
  { v: "veg", l: "Veg", c: "#16a34a" },
  { v: "non_veg", l: "Non-veg", c: "#dc2626" },
  { v: "either", l: "Either", c: "#9ca3af" },
];
const BUDGET_CHIPS = [150, 250, 400];
const VIBE_SUGGESTIONS = ["biryani", "pizza", "chinese", "comfort food", "something light"];

function TagInput({
  tags,
  onChange,
  placeholder,
  variant,
}: {
  tags: string[];
  onChange: (t: string[]) => void;
  placeholder: string;
  variant: "alert" | "muted";
}) {
  const [input, setInput] = useState("");
  function add() {
    const val = input.trim();
    if (val && !tags.includes(val)) onChange([...tags, val]);
    setInput("");
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {tags.map((t) => (
        <span
          key={t}
          className={`flex items-center gap-1 text-xs font-sans px-2 py-0.5 border ${
            variant === "alert"
              ? "border-ink text-ink bg-canvas-soft font-bold"
              : "border-hairline text-ink bg-canvas-soft"
          }`}
        >
          {t}
          <button
            type="button"
            onClick={() => onChange(tags.filter((x) => x !== t))}
            className="opacity-60 hover:opacity-100"
          >
            ×
          </button>
        </span>
      ))}
      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); }
        }}
        onBlur={add}
        placeholder={placeholder}
        className={`text-xs font-sans border-b bg-transparent outline-none py-0.5 min-w-[110px] ${
          variant === "alert" ? "border-ink placeholder-body-muted" : "border-hairline placeholder-body-muted"
        }`}
      />
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div>
        <Label className="font-sans text-[13px] font-bold uppercase tracking-[0.4px] text-ink">
          {label}
        </Label>
        {hint && <p className="font-sans text-xs text-body-muted mt-0.5">{hint}</p>}
      </div>
      {children}
    </div>
  );
}

export default function CravingCardForm({
  initial,
  submitting = false,
  submitLabel = "Submit my card",
  onSubmit,
}: {
  initial?: Partial<CravingCardData>;
  submitting?: boolean;
  submitLabel?: string;
  onSubmit: (d: CravingCardData) => void;
}) {
  const [veg, setVeg] = useState(initial?.veg ?? "either");
  const [budget, setBudget] = useState(initial?.budget_max ? String(initial.budget_max) : "");
  const [cuisineVibe, setCuisineVibe] = useState(initial?.cuisine_vibe ?? "");
  const [mustHave, setMustHave] = useState(initial?.must_have ?? "");
  const [allergies, setAllergies] = useState<string[]>(initial?.allergies ?? []);

  function submit() {
    onSubmit({
      veg,
      budget_max: budget ? parseInt(budget) : null,
      cuisine_vibe: cuisineVibe.trim() || null,
      must_have: mustHave.trim() || null,
      allergies,
      deal_breakers: [],
    });
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Diet */}
      <Field label="Diet">
        <div className="grid grid-cols-3 gap-2">
          {DIET.map(({ v, l, c }) => {
            const on = veg === v;
            return (
              <button
                key={v}
                type="button"
                onClick={() => setVeg(v)}
                className={`flex items-center justify-center gap-1.5 border px-3 py-2 font-sans text-sm transition-colors ${
                  on ? "bg-ink text-canvas border-ink" : "border-hairline text-ink hover:border-ink"
                }`}
              >
                <span
                  className="flex items-center justify-center w-3 h-3 border"
                  style={{ borderColor: on ? "#ffffff" : c }}
                >
                  <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: on ? "#ffffff" : c }} />
                </span>
                {l}
              </button>
            );
          })}
        </div>
      </Field>

      {/* Budget */}
      <Field label="Budget per person">
        <div className="flex items-center gap-2 flex-wrap">
          {BUDGET_CHIPS.map((b) => (
            <button
              key={b}
              type="button"
              onClick={() => setBudget(String(b))}
              className={`text-xs font-sans border px-2.5 py-1 transition-colors ${
                budget === String(b) ? "bg-ink text-canvas border-ink" : "border-hairline text-ink hover:border-ink"
              }`}
            >
              ≤ ₹{b}
            </button>
          ))}
          <button
            type="button"
            onClick={() => setBudget("")}
            className={`text-xs font-sans border px-2.5 py-1 transition-colors ${
              !budget ? "bg-ink text-canvas border-ink" : "border-hairline text-ink hover:border-ink"
            }`}
          >
            Any
          </button>
          <div className="flex items-center gap-1 ml-auto">
            <span className="font-sans text-sm text-body-muted">₹</span>
            <Input
              type="number"
              placeholder="custom"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              className="w-24 h-9"
            />
          </div>
        </div>
      </Field>

      {/* Cuisine vibe */}
      <Field label="Cuisine vibe" hint="What are you in the mood for?">
        <Textarea
          placeholder="e.g. biryani, something light, comfort food"
          value={cuisineVibe}
          onChange={(e) => setCuisineVibe(e.target.value)}
          rows={2}
        />
        <div className="flex flex-wrap gap-1.5">
          {VIBE_SUGGESTIONS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() =>
                setCuisineVibe((v) => (v.trim() ? `${v.trim()}, ${s}` : s))
              }
              className="text-xs font-sans border border-hairline px-2 py-0.5 text-body-muted hover:border-ink hover:text-ink transition-colors"
            >
              + {s}
            </button>
          ))}
        </div>
      </Field>

      {/* Must have */}
      <Field label="Must have" hint="We'll make sure this exact dish lands in your order.">
        <Input
          placeholder="e.g. garlic bread, paneer tikka"
          value={mustHave}
          onChange={(e) => setMustHave(e.target.value)}
        />
      </Field>

      {/* Allergies */}
      <Field label="⚠ Allergies" hint="These will never appear in your order.">
        <TagInput tags={allergies} onChange={setAllergies} placeholder="add allergy, Enter" variant="alert" />
      </Field>

      <Button
        onClick={submit}
        disabled={submitting}
        className="w-full bg-ink text-canvas hover:bg-ink/80 font-sans font-bold mt-1"
      >
        {submitting ? "Submitting…" : submitLabel}
      </Button>
    </div>
  );
}
