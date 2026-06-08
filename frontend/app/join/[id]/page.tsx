"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import confetti from "canvas-confetti";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { supabase } from "@/lib/supabase";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type RoomInfo = { room_code: string; host_user_id: string; status: string };

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
        className={`text-xs font-sans border-b bg-transparent outline-none py-0.5 min-w-[80px] ${
          variant === "alert" ? "border-ink placeholder-body-muted" : "border-hairline"
        }`}
      />
    </div>
  );
}

export default function JoinPage() {
  const { id: roomId } = useParams<{ id: string }>();
  const router = useRouter();

  const [room, setRoom] = useState<RoomInfo | null>(null);
  const [step, setStep] = useState<"name" | "card">("name");
  const [displayName, setDisplayName] = useState("");
  const [participantId, setParticipantId] = useState("");
  const [joining, setJoining] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [kicked, setKicked] = useState(false);
  const pidRef = useRef("");

  // Card fields
  const [veg, setVeg] = useState("either");
  const [budget, setBudget] = useState("");
  const [cuisineVibe, setCuisineVibe] = useState("");
  const [mustHave, setMustHave] = useState("");
  const [allergies, setAllergies] = useState<string[]>([]);
  const [dealBreakers, setDealBreakers] = useState<string[]>([]);

  useEffect(() => {
    fetch(`${API_URL}/rooms/${roomId}`)
      .then((r) => r.json())
      .then((d) => setRoom(d))
      .catch(() => {});
  }, [roomId]);

  // Watch for kick
  useEffect(() => {
    if (!participantId) return;
    pidRef.current = participantId;
    const channel = supabase
      .channel(`participant-${participantId}`)
      .on(
        "postgres_changes",
        { event: "DELETE", schema: "public", table: "participants", filter: `id=eq.${participantId}` },
        () => setKicked(true)
      )
      .subscribe();
    return () => { supabase.removeChannel(channel); };
  }, [participantId]);

  async function handleJoin() {
    if (!displayName.trim()) return;
    setJoining(true);
    const res = await fetch(`${API_URL}/rooms/${roomId}/join`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: displayName.trim() }),
    });
    if (!res.ok) { setJoining(false); return; }
    const data = await res.json();
    setParticipantId(data.participant_id);
    setJoining(false);
    setStep("card");
  }

  async function handleSubmitCard() {
    setSubmitting(true);
    const res = await fetch(`${API_URL}/rooms/${roomId}/cards`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        participant_id: participantId,
        veg,
        budget_max: budget ? parseInt(budget) : null,
        cuisine_vibe: cuisineVibe || null,
        must_have: mustHave || null,
        allergies,
        deal_breakers: dealBreakers,
      }),
    });
    if (!res.ok) { setSubmitting(false); return; }
    confetti({ particleCount: 120, spread: 70, origin: { y: 0.6 } });
    setTimeout(() => router.push(`/room/${roomId}/wait?pid=${participantId}`), 800);
  }

  if (kicked) {
    return (
      <main className="min-h-screen bg-canvas flex flex-col items-center justify-center px-6">
        <h1 className="font-display text-2xl text-ink mb-3">You've been removed</h1>
        <p className="font-sans text-sm text-body-muted">The host removed you from this circle.</p>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-canvas flex flex-col items-center justify-start px-4 py-10">
      <p className="text-[11px] font-sans font-bold tracking-[0.4px] uppercase text-body-muted mb-8">
        Circle
      </p>

      <AnimatePresence mode="wait">
        {step === "name" && (
          <motion.div
            key="name"
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, x: "-100%" }}
            className="w-full max-w-sm flex flex-col gap-5"
          >
            <div className="text-center">
              <p className="font-sans text-body-muted text-sm mb-1">You're invited</p>
              <h1 className="font-display text-3xl text-ink leading-tight">
                {room ? `Room ${room.room_code}` : "Join the circle"}
              </h1>
            </div>
            <div className="w-12 border-t border-hairline mx-auto" />
            <div className="flex flex-col gap-2">
              <Label htmlFor="name" className="font-sans text-sm text-ink">
                What should we call you?
              </Label>
              <Input
                id="name"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleJoin()}
                placeholder="Your name"
                autoFocus
              />
            </div>
            <Button
              onClick={handleJoin}
              disabled={!displayName.trim() || joining}
              className="w-full bg-ink text-canvas hover:bg-ink/80 font-sans font-bold"
            >
              {joining ? "Joining…" : "Join the circle →"}
            </Button>
          </motion.div>
        )}

        {step === "card" && (
          <motion.div
            key="card"
            initial={{ opacity: 0, x: "100%" }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ type: "spring", stiffness: 300, damping: 30 }}
            className="w-full max-w-sm flex flex-col gap-5 pb-16"
          >
            <h1 className="font-display text-2xl text-ink">Your craving card</h1>
            <div className="border-t border-hairline" />

            {/* Diet */}
            <div className="flex flex-col gap-2">
              <Label className="font-sans text-sm font-bold text-ink">Diet</Label>
              <ToggleGroup
                type="single"
                value={veg}
                onValueChange={(v) => v && setVeg(v)}
                className="justify-start gap-2"
              >
                {[
                  { value: "veg", label: "Veg" },
                  { value: "non_veg", label: "Non-veg" },
                  { value: "either", label: "Either" },
                ].map(({ value, label }) => (
                  <ToggleGroupItem
                    key={value}
                    value={value}
                    className="border border-ink font-sans text-sm px-4 data-[state=on]:bg-ink data-[state=on]:text-canvas"
                  >
                    {label}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            </div>

            {/* Budget */}
            <div className="flex flex-col gap-2">
              <Label htmlFor="budget" className="font-sans text-sm font-bold text-ink">
                Budget per person
              </Label>
              <div className="flex items-center gap-1.5">
                <span className="font-sans text-sm text-body-muted">₹</span>
                <Input
                  id="budget"
                  type="number"
                  placeholder="any"
                  value={budget}
                  onChange={(e) => setBudget(e.target.value)}
                  className="w-28"
                />
              </div>
            </div>

            {/* Cuisine vibe */}
            <div className="flex flex-col gap-2">
              <Label htmlFor="vibe" className="font-sans text-sm font-bold text-ink">
                Cuisine vibe
              </Label>
              <Textarea
                id="vibe"
                placeholder="e.g. biryani, something light, comfort food"
                value={cuisineVibe}
                onChange={(e) => setCuisineVibe(e.target.value)}
                rows={2}
              />
            </div>

            {/* Must have */}
            <div className="flex flex-col gap-2">
              <Label htmlFor="must" className="font-sans text-sm font-bold text-ink">
                Must have
              </Label>
              <Input
                id="must"
                placeholder="e.g. chicken, extra spicy"
                value={mustHave}
                onChange={(e) => setMustHave(e.target.value)}
              />
            </div>

            {/* Allergies */}
            <div className="flex flex-col gap-2">
              <Label className="font-sans text-sm font-bold text-ink">
                ⚠ Allergies — these will never appear in your order
              </Label>
              <TagInput
                tags={allergies}
                onChange={setAllergies}
                placeholder="add allergy, press Enter"
                variant="alert"
              />
            </div>

            {/* Deal breakers */}
            <div className="flex flex-col gap-2">
              <Label className="font-sans text-sm font-bold text-ink">
                Deal breakers
              </Label>
              <TagInput
                tags={dealBreakers}
                onChange={setDealBreakers}
                placeholder="add item, press Enter"
                variant="muted"
              />
            </div>

            <Button
              onClick={handleSubmitCard}
              disabled={submitting}
              className="w-full bg-ink text-canvas hover:bg-ink/80 font-sans font-bold mt-2"
            >
              {submitting ? "Submitting…" : "Submit my card ✓"}
            </Button>
          </motion.div>
        )}
      </AnimatePresence>
    </main>
  );
}
