"use client";

import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { motion } from "framer-motion";
import { supabase } from "@/lib/supabase";
import PlanReveal from "@/components/PlanReveal";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const PLAN_STATUSES = ["discovering", "choosing", "ordering", "tracking", "done"];

export default function WaitPage() {
  const { id: roomId } = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const participantId = searchParams.get("pid") ?? "";

  const [participants, setParticipants] = useState<{ display_name: string; has_card: boolean }[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [kicked, setKicked] = useState(false);
  const [status, setStatus] = useState<string>("collecting");

  useEffect(() => {
    fetch(`${API_URL}/rooms/${roomId}`)
      .then((r) => r.json())
      .then((d) => {
        setParticipants(d.participants ?? []);
        setTotalCount(d.total_participants ?? 0);
        setStatus(d.status ?? "collecting");
      })
      .catch(() => {});
  }, [roomId]);

  // Poll room status so guests advance into the plan view
  useEffect(() => {
    const t = setInterval(() => {
      fetch(`${API_URL}/rooms/${roomId}`)
        .then((r) => r.json())
        .then((d) => setStatus(d.status ?? "collecting"))
        .catch(() => {});
    }, 3000);
    return () => clearInterval(t);
  }, [roomId]);

  useEffect(() => {
    const channel = supabase
      .channel(`wait-${roomId}`)
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "participants", filter: `room_id=eq.${roomId}` },
        (payload) => {
          const p = payload.new as { display_name: string };
          setParticipants((prev) => [...prev, { display_name: p.display_name, has_card: false }]);
          setTotalCount((n) => n + 1);
        }
      )
      .on(
        "postgres_changes",
        { event: "DELETE", schema: "public", table: "participants", filter: `room_id=eq.${roomId}` },
        (payload) => {
          const deleted = payload.old as { id: string };
          if (deleted.id === participantId) setKicked(true);
          // re-fetch to keep list accurate after any kick
          fetch(`${API_URL}/rooms/${roomId}`)
            .then((r) => r.json())
            .then((d) => { setParticipants(d.participants ?? []); setTotalCount(d.total_participants ?? 0); })
            .catch(() => {});
        }
      )
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "craving_cards", filter: `room_id=eq.${roomId}` },
        () => {
          fetch(`${API_URL}/rooms/${roomId}`)
            .then((r) => r.json())
            .then((d) => setParticipants(d.participants ?? []))
            .catch(() => {});
        }
      )
      .on(
        "postgres_changes",
        { event: "UPDATE", schema: "public", table: "rooms", filter: `id=eq.${roomId}` },
        (payload) => {
          const room = payload.new as { status: string };
          if (room.status) setStatus(room.status);
        }
      )
      .subscribe();

    return () => { supabase.removeChannel(channel); };
  }, [roomId, participantId]);

  const cardsIn = participants.filter((p) => p.has_card).length;
  const progress = totalCount > 0 ? cardsIn / totalCount : 0;

  if (kicked) {
    return (
      <main className="min-h-screen bg-canvas flex flex-col items-center justify-center px-6">
        <h1 className="font-display text-2xl text-ink mb-3">You've been removed</h1>
        <p className="font-sans text-sm text-body-muted">The host removed you from this circle.</p>
      </main>
    );
  }

  // Once the agent starts planning, guests see + vote on the plans
  if (PLAN_STATUSES.includes(status)) {
    return (
      <main className="min-h-screen bg-canvas">
        <header className="flex items-center justify-center px-6 py-4 border-b border-hairline">
          <span className="font-sans font-bold text-sm tracking-[0.3px] text-ink">Circle</span>
        </header>
        <PlanReveal roomId={roomId} participantId={participantId} isHost={false} />
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-canvas flex flex-col items-center justify-center px-6">
      <p className="text-[11px] font-sans font-bold tracking-[0.4px] uppercase text-body-muted mb-10">
        Circle
      </p>

      <div className="w-full max-w-xs flex flex-col items-center gap-6">
        <motion.div
          className="w-16 h-16 border border-ink"
          animate={{ rotate: 360 }}
          transition={{ repeat: Infinity, duration: 3, ease: "linear" }}
        />

        <h1 className="font-display text-2xl text-ink text-center">
          Your card is in.
        </h1>
        <p className="font-sans text-sm text-body-muted text-center">
          Waiting for everyone to submit their craving cards.
        </p>

        {/* Progress bar */}
        <div className="w-full">
          <div className="w-full h-1 bg-hairline">
            <motion.div
              className="h-1 bg-ink"
              initial={{ width: 0 }}
              animate={{ width: `${progress * 100}%` }}
              transition={{ duration: 0.4 }}
            />
          </div>
          <p className="text-xs text-body-muted font-sans mt-1.5 text-right">
            {cardsIn} of {totalCount} cards in
          </p>
        </div>

        {/* Participant dots */}
        {participants.length > 0 && (
          <div className="flex flex-wrap gap-3 justify-center mt-2">
            {participants.map((p, i) => (
              <motion.div
                key={i}
                initial={{ opacity: 0, scale: 0.6 }}
                animate={{ opacity: 1, scale: 1 }}
                className="flex flex-col items-center gap-1"
              >
                <div
                  className={`w-2.5 h-2.5 border ${
                    p.has_card ? "bg-ink border-ink" : "bg-canvas border-ink animate-pulse"
                  }`}
                />
                <span className="text-[10px] font-sans text-body-muted">{p.display_name}</span>
              </motion.div>
            ))}
          </div>
        )}
      </div>
    </main>
  );
}
