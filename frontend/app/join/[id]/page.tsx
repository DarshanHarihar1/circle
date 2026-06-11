"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import confetti from "canvas-confetti";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { supabase } from "@/lib/supabase";
import CravingCardForm, { type CravingCardData } from "@/components/CravingCardForm";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type RoomInfo = { room_code: string; host_user_id: string; status: string };

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

  async function handleSubmitCard(card: CravingCardData) {
    setSubmitting(true);
    const res = await fetch(`${API_URL}/rooms/${roomId}/cards`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_id: participantId, ...card }),
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
            <div>
              <h1 className="font-display text-2xl text-ink">Your craving card</h1>
              <p className="font-sans text-sm text-body-muted mt-1">
                Tell the circle what you&apos;re after — we&apos;ll do the rest.
              </p>
            </div>
            <div className="border-t border-hairline" />

            <CravingCardForm
              submitting={submitting}
              submitLabel="Submit my card ✓"
              onSubmit={handleSubmitCard}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </main>
  );
}
