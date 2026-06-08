"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Button } from "@/components/ui/button";
import { supabase } from "@/lib/supabase";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type SubOrderItem = { participant_id: string; item_name: string; price: number };
type SubOrder = { restaurant_id: string; restaurant_name: string; items: SubOrderItem[]; coupon_code: string | null };
type Plan = {
  id: string;
  kind: "single" | "multi";
  sub_orders: SubOrder[];
  total: number;
  satisfaction: number | null;
  n_deliveries: number;
  notes: string | null;
  rationale: string | null;
  why_not_runner_up: string | null;
  per_person_fit: Record<string, string>;
  rank: number | null;
  chosen: boolean;
};
type Vote = { participant_id: string; plan_id: string };

const MEDALS = ["🥇", "🥈", "🥉"];
const LOADING_STEPS = ["Discovering restaurants", "Checking menus", "Scoring dishes", "Building plans"];

export default function PlanReveal({
  roomId,
  participantId,
  isHost,
  nameByPid = {},
}: {
  roomId: string;
  participantId: string;
  isHost: boolean;
  nameByPid?: Record<string, string>;
}) {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [votes, setVotes] = useState<Vote[]>([]);
  const [names, setNames] = useState<Record<string, string>>(nameByPid);
  const [loadingStep, setLoadingStep] = useState(0);
  const [choosing, setChoosing] = useState<string | null>(null);
  const [openWhyNot, setOpenWhyNot] = useState<Record<string, boolean>>({});
  const ready = plans.length > 0;

  const fetchPlans = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/rooms/${roomId}/plans`);
      if (!res.ok) return;
      const data = await res.json();
      if (Array.isArray(data.plans)) setPlans(data.plans);
      if (Array.isArray(data.votes)) setVotes(data.votes);
    } catch {
      /* silent */
    }
  }, [roomId]);

  // Resolve participant names if not provided
  useEffect(() => {
    if (Object.keys(names).length) return;
    fetch(`${API_URL}/rooms/${roomId}`)
      .then((r) => r.json())
      .then((d) => {
        const map: Record<string, string> = {};
        for (const p of d.participants ?? []) map[p.id] = p.display_name;
        setNames(map);
      })
      .catch(() => {});
  }, [roomId, names]);

  // Cycle the loading label until plans arrive
  useEffect(() => {
    if (ready) return;
    const t = setInterval(() => setLoadingStep((s) => (s + 1) % LOADING_STEPS.length), 4000);
    return () => clearInterval(t);
  }, [ready]);

  const chosen = plans.find((p) => p.chosen);

  // Poll + realtime. Stop the 3s poll once a plan is locked in — realtime still
  // covers the (terminal) chosen flip, so we don't need to keep hammering the API.
  useEffect(() => {
    fetchPlans();
    const poll = chosen ? null : setInterval(fetchPlans, 3000);
    const channel = supabase
      .channel(`plans-${roomId}`)
      .on("postgres_changes", { event: "*", schema: "public", table: "plans", filter: `room_id=eq.${roomId}` }, fetchPlans)
      .on("postgres_changes", { event: "*", schema: "public", table: "plan_votes" }, fetchPlans)
      .subscribe();
    return () => {
      if (poll) clearInterval(poll);
      supabase.removeChannel(channel);
    };
  }, [roomId, fetchPlans, chosen]);

  const myVote = votes.find((v) => v.participant_id === participantId);

  async function vote(planId: string) {
    if (!participantId) return;
    // optimistic
    setVotes((prev) => [...prev.filter((v) => v.participant_id !== participantId), { participant_id: participantId, plan_id: planId }]);
    await fetch(`${API_URL}/rooms/${roomId}/vote`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_id: participantId, plan_id: planId }),
    }).catch(() => {});
    fetchPlans();
  }

  async function choose(planId: string) {
    setChoosing(planId);
    await fetch(`${API_URL}/rooms/${roomId}/choose-plan`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: planId }),
    }).catch(() => {});
    fetchPlans();
  }

  const conflict = plans.find((p) => p.notes);

  // ── Loading state ──
  if (!ready) {
    return (
      <div className="flex flex-col items-center justify-center gap-6 py-20">
        <motion.div
          className="w-12 h-12 border border-ink"
          animate={{ rotate: 360 }}
          transition={{ repeat: Infinity, duration: 2.5, ease: "linear" }}
        />
        <p className="font-display text-xl text-ink text-center">Finding the best options for your circle…</p>
        <AnimatePresence mode="wait">
          <motion.p
            key={loadingStep}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            className="font-sans text-sm text-body-muted"
          >
            {LOADING_STEPS[loadingStep]}…
          </motion.p>
        </AnimatePresence>
      </div>
    );
  }

  return (
    <div className="w-full max-w-2xl mx-auto px-4 py-8 flex flex-col gap-6">
      <div className="text-center">
        <h1 className="font-display text-3xl text-ink">
          {chosen ? "Plan locked in" : "Pick your plan"}
        </h1>
        <p className="font-sans text-sm text-body-muted mt-1">
          {chosen ? "The host chose this plan for everyone." : "Vote for your favourite — the host makes the final call."}
        </p>
      </div>

      {conflict && (
        <div className="border border-ink p-3">
          <p className="font-sans text-xs uppercase tracking-wide text-body-muted mb-1">Heads up</p>
          <p className="font-sans text-sm text-ink">
            Some preferences couldn&apos;t be fully met nearby — flagged items will be confirmed before ordering.
          </p>
        </div>
      )}

      <div className="flex flex-col gap-5">
        {plans.map((plan, i) => {
          const planVotes = votes.filter((v) => v.plan_id === plan.id);
          const voted = myVote?.plan_id === plan.id;
          const title = plan.sub_orders.map((s) => s.restaurant_name).join(" + ");
          const sat = Math.round((plan.satisfaction ?? 0) * 100);
          return (
            <motion.div
              key={plan.id}
              initial={{ opacity: 0, y: 40 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.15 }}
              className={`border-b border-hairline pb-5 ${plan.chosen ? "bg-canvas-soft -mx-4 px-4 pt-4" : ""}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span className="text-lg">{MEDALS[i] ?? "•"}</span>
                  <h2 className="font-display text-xl text-ink leading-tight">{title}</h2>
                </div>
                {!chosen && (
                  <Button
                    size="sm"
                    onClick={() => vote(plan.id)}
                    disabled={!participantId}
                    className={
                      voted
                        ? "bg-ink text-canvas font-sans text-xs"
                        : "bg-canvas text-ink border border-ink hover:bg-ink hover:text-canvas font-sans text-xs"
                    }
                  >
                    {voted ? "Voted ✓" : "Vote →"}
                  </Button>
                )}
              </div>

              <p className="font-sans text-sm text-body-muted mt-1.5">
                ₹{plan.total} · {plan.n_deliveries} {plan.n_deliveries === 1 ? "delivery" : "deliveries"}
                {plan.sub_orders.some((s) => s.coupon_code) && (
                  <> · coupon {plan.sub_orders.find((s) => s.coupon_code)?.coupon_code}</>
                )}
              </p>

              {/* Satisfaction bar — bg-ink fill on bg-hairline track */}
              <div className="mt-3 flex items-center gap-2">
                <span className="font-sans text-xs text-body-muted w-20">Satisfaction</span>
                <div className="flex-1 h-1.5 bg-hairline">
                  <motion.div
                    className="h-1.5 bg-ink"
                    initial={{ width: 0 }}
                    animate={{ width: `${sat}%` }}
                    transition={{ duration: 0.6, delay: i * 0.15 + 0.2 }}
                  />
                </div>
                <span className="font-sans text-xs text-ink w-8 text-right">{sat}%</span>
              </div>

              {/* Rationale — Lora serif italic */}
              {plan.rationale && (
                <p className="font-body italic text-[15px] text-ink/80 mt-3 leading-snug">
                  &ldquo;{plan.rationale}&rdquo;
                </p>
              )}

              {/* Why not runner-up — collapsible */}
              {plan.why_not_runner_up && (
                <div className="mt-2">
                  <button
                    onClick={() => setOpenWhyNot((p) => ({ ...p, [plan.id]: !p[plan.id] }))}
                    className="font-sans text-xs text-body-muted hover:text-ink"
                  >
                    {openWhyNot[plan.id] ? "▾" : "▸"} Why not the runner-up?
                  </button>
                  <AnimatePresence>
                    {openWhyNot[plan.id] && (
                      <motion.p
                        initial={{ opacity: 0, height: 0 }}
                        animate={{ opacity: 1, height: "auto" }}
                        exit={{ opacity: 0, height: 0 }}
                        className="font-body italic text-sm text-body-muted mt-1 overflow-hidden"
                      >
                        {plan.why_not_runner_up}
                      </motion.p>
                    )}
                  </AnimatePresence>
                </div>
              )}

              {/* Per-person chips */}
              <div className="mt-3 flex flex-wrap gap-1.5">
                {Object.entries(plan.per_person_fit).map(([pid, fit]) => (
                  <span
                    key={pid}
                    title={`${names[pid] ?? "Guest"} → ${fit}`}
                    className="flex items-center gap-1 text-xs font-sans border border-hairline px-2 py-0.5"
                  >
                    <span className="w-1.5 h-1.5 rounded-full bg-ink" />
                    {names[pid] ?? "Guest"}
                  </span>
                ))}
              </div>

              {/* Live votes */}
              {planVotes.length > 0 && (
                <div className="mt-3 flex items-center gap-1.5">
                  <span className="font-sans text-xs text-body-muted">Votes</span>
                  <AnimatePresence>
                    {planVotes.map((v) => (
                      <motion.span
                        key={v.participant_id}
                        initial={{ scale: 0 }}
                        animate={{ scale: 1 }}
                        className="w-4 h-4 rounded-full bg-ink flex items-center justify-center text-[8px] text-canvas font-sans"
                        title={names[v.participant_id] ?? "Guest"}
                      >
                        {(names[v.participant_id] ?? "G")[0]}
                      </motion.span>
                    ))}
                  </AnimatePresence>
                  <span className="font-sans text-xs text-body-muted ml-1">({planVotes.length})</span>
                </div>
              )}

              {/* Host choose */}
              {isHost && !chosen && (
                <div className="mt-4">
                  <Button
                    onClick={() => choose(plan.id)}
                    disabled={choosing === plan.id}
                    className="w-full bg-ink text-canvas hover:bg-ink/80 font-sans font-bold"
                  >
                    {choosing === plan.id ? "Locking in…" : `Choose ${title} for the group`}
                  </Button>
                </div>
              )}

              {plan.chosen && (
                <p className="font-sans text-sm font-bold text-ink mt-3">✓ Chosen for the group</p>
              )}
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}
