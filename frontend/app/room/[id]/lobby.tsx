"use client";

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { QRCodeSVG } from "qrcode.react";
import { toast, Toaster } from "sonner";
import { Check, Copy, ExternalLink, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { supabase } from "@/lib/supabase";
import PlanReveal from "@/components/PlanReveal";
import CartReview from "@/components/CartReview";
import OrderTracking from "@/components/OrderTracking";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Participant = {
  id: string;
  display_name: string;
  is_host: boolean;
  has_card: boolean;
};

type RoomState = {
  id: string;
  status: string;
  address_id: string | null;
  host_user_id: string;
  room_code: string;
  participants: Participant[];
  cards_submitted: number;
  total_participants: number;
};

type Address = {
  id: string;
  label: string;
  address: string;
};

type PrefSpec = {
  id: string;
  participant_id: string;
  veg: string;
  budget_max: number | null;
  allergies: string[];
  excludes: string[];
  soft: string[];
  approved: boolean;
};

const AGENT_STATUSES = ["activated", "planning", "discovering", "choosing", "ordering", "confirming", "tracking", "done"];

const STATUS_LABEL: Record<string, string> = {
  activated: "Parsing preferences…",
  planning: "Preferences parsed — waiting for approval",
  discovering: "Discovering restaurants…",
  choosing: "Building plans…",
  ordering: "Building cart…",
  confirming: "Cart ready — review and place order",
  tracking: "Order placed — tracking delivery",
};

export default function Lobby({
  initialRoom,
  roomId,
}: {
  initialRoom: RoomState;
  roomId: string;
}) {
  const [participants, setParticipants] = useState<Participant[]>(initialRoom.participants);
  const [roomStatus, setRoomStatus] = useState(initialRoom.status);
  const [addressSheetOpen, setAddressSheetOpen] = useState(false);
  const [addresses, setAddresses] = useState<Address[]>([]);
  const [selectedAddress, setSelectedAddress] = useState<Address | null>(null);
  const [loadingAddresses, setLoadingAddresses] = useState(false);
  const [activating, setActivating] = useState(false);
  const [prefSpecs, setPrefSpecs] = useState<PrefSpec[]>([]);
  const [approving, setApproving] = useState(false);

  const participantsRef = useRef(participants);
  participantsRef.current = participants;

  const joinUrl = `${typeof window !== "undefined" ? window.location.origin : ""}/join/${roomId}`;
  const cardsSubmitted = participants.filter((p) => p.has_card).length;
  const allSubmitted = participants.length > 0 && cardsSubmitted === participants.length;
  const agentRunning = AGENT_STATUSES.includes(roomStatus);
  const showPlans = ["discovering", "choosing"].includes(roomStatus);
  const showCartLoading = roomStatus === "ordering";
  const showCart = roomStatus === "confirming";
  const showTracking = ["tracking", "done"].includes(roomStatus);
  const hostParticipantId = participants.find((p) => p.is_host)?.id ?? "";
  const nameByPid = Object.fromEntries(participants.map((p) => [p.id, p.display_name]));

  // Fetch pref specs when status = planning
  async function fetchPrefSpecs() {
    try {
      const res = await fetch(`${API_URL}/rooms/${roomId}/pref-specs`, { credentials: "include" });
      if (!res.ok) return;
      const data = await res.json();
      if (data.pref_specs?.length) setPrefSpecs(data.pref_specs);
    } catch { /* silent */ }
  }

  // Poll room status while agent is running
  useEffect(() => {
    if (!AGENT_STATUSES.includes(roomStatus)) return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_URL}/rooms/${roomId}`, { credentials: "include" });
        if (!res.ok) return;
        const data = await res.json();
        if (data.status !== roomStatus) {
          setRoomStatus(data.status);
          if (data.status === "planning") fetchPrefSpecs();
        }
      } catch { /* silent */ }
    }, 2500);
    return () => clearInterval(interval);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roomStatus, roomId]);

  // Supabase Realtime — participants + cards
  useEffect(() => {
    const channel = supabase
      .channel(`room-${roomId}`)
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "participants", filter: `room_id=eq.${roomId}` },
        (payload) => {
          const p = payload.new as Participant;
          setParticipants((prev) =>
            prev.find((x) => x.id === p.id) ? prev : [...prev, { ...p, has_card: false }]
          );
          toast(`${p.display_name} joined the circle`);
        }
      )
      .on(
        "postgres_changes",
        { event: "DELETE", schema: "public", table: "participants", filter: `room_id=eq.${roomId}` },
        (payload) => {
          setParticipants((prev) =>
            prev.filter((p) => p.id !== (payload.old as { id: string }).id)
          );
        }
      )
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "craving_cards", filter: `room_id=eq.${roomId}` },
        (payload) => {
          const card = payload.new as { participant_id: string };
          setParticipants((prev) =>
            prev.map((p) => p.id === card.participant_id ? { ...p, has_card: true } : p)
          );
          const who = participantsRef.current.find((p) => p.id === card.participant_id);
          if (who) toast(`${who.display_name} submitted their card`);
        }
      )
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "pref_specs", filter: `room_id=eq.${roomId}` },
        () => {
          fetchPrefSpecs();
          setRoomStatus("planning");
        }
      )
      .on(
        "postgres_changes",
        { event: "UPDATE", schema: "public", table: "rooms", filter: `id=eq.${roomId}` },
        (payload) => {
          // Authoritative status source — the agent advances the room server-side
          // through planning → discovering → … → done. Relying only on the
          // optimistic local set + a poll (which is gated off in `collecting`)
          // would leave the host stuck if the transition happened elsewhere.
          const room = payload.new as { status?: string };
          if (room.status) {
            setRoomStatus(room.status);
            if (room.status === "planning") fetchPrefSpecs();
          }
        }
      )
      .subscribe();
    return () => { supabase.removeChannel(channel); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roomId]);

  async function openAddressPicker() {
    setAddressSheetOpen(true);
    if (addresses.length) return;
    setLoadingAddresses(true);
    try {
      const res = await fetch(`${API_URL}/rooms/${roomId}/addresses`, { credentials: "include" });
      if (!res.ok) throw new Error();
      const data = await res.json();
      const raw: string = data.addresses?.[0]?.text ?? "";
      const parsed: Address[] = [];
      const lines = raw.split("\n").filter((l: string) => /^\d+\./.test(l));
      for (const line of lines) {
        const idMatch = line.match(/\(ID:\s*([^)]+)\)/);
        const labelMatch = line.match(/\[([^\]]+)\]/);
        const addrMatch = line.match(/:\s(.+?)\s*\(ID:/);
        if (idMatch) {
          parsed.push({
            id: idMatch[1].trim(),
            label: labelMatch?.[1] ?? "Address",
            address: addrMatch?.[1]?.trim() ?? "",
          });
        }
      }
      setAddresses(parsed);
    } catch {
      toast.error("Could not load addresses");
    } finally {
      setLoadingAddresses(false);
    }
  }

  async function pickAddress(addr: Address) {
    await fetch(`${API_URL}/rooms/${roomId}/address`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ address_id: addr.id }),
    });
    setSelectedAddress(addr);
    setAddressSheetOpen(false);
  }

  async function kickParticipant(pid: string) {
    await fetch(`${API_URL}/rooms/${roomId}/participants/${pid}`, {
      method: "DELETE",
      credentials: "include",
    });
  }

  async function handleActivate() {
    setActivating(true);
    try {
      const res = await fetch(`${API_URL}/rooms/${roomId}/activate`, {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        toast.error(body.detail || "Could not activate");
        return;
      }
      setRoomStatus("activated");
      toast("Agent started — parsing preferences…");
    } catch {
      toast.error("Network error");
    } finally {
      setActivating(false);
    }
  }

  async function handleApprovePrefs() {
    setApproving(true);
    try {
      const res = await fetch(`${API_URL}/rooms/${roomId}/approve-prefs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ edits: [] }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        toast.error(body.detail || "Could not approve");
        return;
      }
      setRoomStatus("discovering");
      toast("Preferences approved — discovering restaurants…");
    } catch {
      toast.error("Network error");
    } finally {
      setApproving(false);
    }
  }

  function copyLink() {
    navigator.clipboard.writeText(joinUrl);
    toast("Link copied");
  }

  const vegLabel: Record<string, string> = {
    veg: "Veg only",
    non_veg: "Non-veg",
    either: "Either",
  };

  return (
    <div className="min-h-screen bg-canvas flex flex-col">
      <Toaster position="top-right" />

      {/* Header */}
      <header className="flex items-center justify-between px-6 py-4 border-b border-hairline">
        <span className="font-sans font-bold text-sm tracking-[0.3px] text-ink">Circle</span>
        <button
          onClick={openAddressPicker}
          className="flex items-center gap-1.5 text-sm font-sans text-body-muted hover:text-ink transition-colors"
        >
          {selectedAddress ? (
            <span className="border border-hairline px-2 py-0.5 text-xs font-sans">{selectedAddress.label}</span>
          ) : (
            <span className="text-xs font-sans text-body-muted">Set delivery address</span>
          )}
          <ExternalLink size={12} />
        </button>
      </header>

      {/* Agent progress banner */}
      <AnimatePresence>
        {agentRunning && (
          <motion.div
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            className="border-b border-hairline px-6 py-3 flex items-center gap-3 bg-canvas"
          >
            <span className="w-2 h-2 rounded-full bg-ink animate-pulse" />
            <span className="font-sans text-sm text-body-muted">
              {STATUS_LABEL[roomStatus] ?? roomStatus}
            </span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Pref approval panel */}
      <AnimatePresence>
        {roomStatus === "planning" && prefSpecs.length > 0 && (
          <motion.section
            key="prefs-panel"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className="border-b border-hairline"
          >
            <div className="px-6 pt-5 pb-2">
              <p className="font-display text-xl text-ink mb-1">Preferences parsed</p>
              <p className="font-sans text-sm text-body-muted">
                Review what the agent understood from each card. Approve to continue.
              </p>
            </div>

            <div className="divide-y divide-hairline">
              {prefSpecs.map((spec) => {
                const participant = participants.find((p) => p.id === spec.participant_id);
                return (
                  <div key={spec.id} className="px-6 py-4">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="font-sans font-bold text-sm text-ink">
                        {participant?.display_name ?? spec.participant_id.slice(0, 8)}
                      </span>
                      <span className="text-xs border border-hairline px-1.5 py-0.5 font-sans text-body-muted">
                        {vegLabel[spec.veg] ?? spec.veg}
                      </span>
                      {spec.budget_max && (
                        <span className="text-xs border border-hairline px-1.5 py-0.5 font-sans text-body-muted">
                          ≤ ₹{spec.budget_max}
                        </span>
                      )}
                    </div>

                    {spec.allergies.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mb-1.5">
                        <span className="text-xs font-sans text-ink font-bold">Allergies:</span>
                        {spec.allergies.map((a) => (
                          <span
                            key={a}
                            className="text-xs border border-ink text-ink bg-canvas-soft font-bold px-1.5 py-0.5 font-sans"
                          >
                            {a}
                          </span>
                        ))}
                      </div>
                    )}

                    {spec.excludes.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mb-1.5">
                        <span className="text-xs font-sans text-body-muted">Excludes:</span>
                        {spec.excludes.map((e) => (
                          <span
                            key={e}
                            className="text-xs border border-hairline text-body-muted px-1.5 py-0.5 font-sans"
                          >
                            {e}
                          </span>
                        ))}
                      </div>
                    )}

                    {spec.soft.length > 0 && (
                      <div className="flex flex-wrap gap-1.5">
                        <span className="text-xs font-sans text-body-muted">Vibes:</span>
                        {spec.soft.map((s) => (
                          <span
                            key={s}
                            className="text-xs bg-canvas-soft border border-hairline px-1.5 py-0.5 font-sans text-body-muted"
                          >
                            {s}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="px-6 py-4 flex justify-end">
              <Button
                onClick={handleApprovePrefs}
                disabled={approving}
                className="bg-ink text-canvas hover:bg-ink/80 font-sans font-bold px-8 gap-2 disabled:opacity-50"
              >
                {approving ? (
                  "Approving…"
                ) : (
                  <>
                    <Check size={14} />
                    Approve — find restaurants
                  </>
                )}
              </Button>
            </div>
          </motion.section>
        )}
      </AnimatePresence>

      {/* Plan reveal + voting (discovering → choosing) */}
      {showPlans && (
        <PlanReveal
          roomId={roomId}
          participantId={hostParticipantId}
          isHost={true}
          nameByPid={nameByPid}
        />
      )}

      {/* Cart building spinner */}
      {showCartLoading && (
        <div className="flex flex-col items-center justify-center gap-4 py-16">
          <motion.div
            className="w-12 h-12 border border-ink"
            animate={{ rotate: 360 }}
            transition={{ repeat: Infinity, duration: 2.5, ease: "linear" }}
          />
          <p className="font-display text-xl text-ink">Building your cart…</p>
          <p className="font-sans text-sm text-body-muted">Checking item availability and pricing</p>
        </div>
      )}

      {/* Cart review + order placement (confirming) */}
      {showCart && (
        <CartReview
          roomId={roomId}
          isHost={true}
          hostParticipantId={hostParticipantId}
          nameByPid={nameByPid}
        />
      )}

      {/* Live order tracking (tracking → done) */}
      {showTracking && (
        <OrderTracking
          roomId={roomId}
          isHost={true}
          hostParticipantId={hostParticipantId}
          nameByPid={nameByPid}
        />
      )}

      {/* Body — only show collecting view when still in collecting state */}
      {!agentRunning && (
        <div className="flex flex-1 flex-col md:flex-row divide-y md:divide-y-0 md:divide-x divide-hairline">
          {/* Left — QR + code */}
          <div className="flex flex-col items-center justify-center gap-6 p-8 md:flex-1">
            <QRCodeSVG value={joinUrl} size={200} />
            <div className="text-center">
              <p className="text-xs font-sans text-body-muted uppercase tracking-widest mb-1">Room code</p>
              <p className="font-display text-2xl tracking-wider text-ink">{initialRoom.room_code}</p>
            </div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={copyLink}
                className="border-ink font-sans text-xs gap-1.5"
              >
                <Copy size={12} /> Copy link
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  if (navigator.share) navigator.share({ url: joinUrl });
                  else copyLink();
                }}
                className="border-ink font-sans text-xs gap-1.5"
              >
                <ExternalLink size={12} /> Share
              </Button>
            </div>
          </div>

          {/* Right — participants */}
          <div className="flex flex-col p-6 md:w-72 gap-4">
            <div className="flex items-center justify-between">
              <span className="font-sans font-bold text-sm text-ink">Participants</span>
              <span className="text-xs text-body-muted font-sans">
                {cardsSubmitted} / {participants.length}
              </span>
            </div>

            <ul className="flex flex-col gap-2 flex-1">
              <AnimatePresence initial={false}>
                {participants.map((p) => (
                  <motion.li
                    key={p.id}
                    layout
                    initial={{ opacity: 0, y: 12 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, x: -20, height: 0 }}
                    transition={{ duration: 0.2 }}
                    className="flex items-center justify-between group"
                  >
                    <div className="flex items-center gap-2">
                      <span className={`w-2 h-2 rounded-full ${p.has_card ? "bg-ink" : "bg-hairline animate-pulse"}`} />
                      <span className="font-sans text-sm text-ink">
                        {p.display_name}
                        {p.is_host && <span className="ml-1 text-xs text-body-muted">(you)</span>}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      {p.has_card && (
                        <motion.span
                          initial={{ opacity: 0, scale: 0.5 }}
                          animate={{ opacity: 1, scale: 1 }}
                          className="text-xs text-ink font-sans"
                        >
                          ✓
                        </motion.span>
                      )}
                      {!p.is_host && (
                        <button
                          onClick={() => kickParticipant(p.id)}
                          className="opacity-0 group-hover:opacity-100 transition-opacity text-body-muted hover:text-ink"
                          title="Remove"
                        >
                          <X size={13} />
                        </button>
                      )}
                    </div>
                  </motion.li>
                ))}
              </AnimatePresence>
            </ul>

            <Progress
              value={participants.length ? (cardsSubmitted / participants.length) * 100 : 0}
              className="h-1.5"
            />
            <p className="text-xs text-body-muted font-sans text-right">
              {cardsSubmitted} of {participants.length} cards in
            </p>
          </div>
        </div>
      )}

      {/* Footer — Activate */}
      {!agentRunning && (
        <div className="border-t border-hairline p-4 flex justify-center">
          <motion.div
            animate={allSubmitted ? { scale: [1, 1.03, 1] } : { scale: 1 }}
            transition={allSubmitted ? { repeat: Infinity, duration: 1.8 } : {}}
          >
            <Button
              disabled={!allSubmitted || activating}
              onClick={handleActivate}
              className="bg-ink text-canvas hover:bg-ink/80 font-sans font-bold px-8 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {activating
                ? "Starting…"
                : allSubmitted
                ? "Activate — find our restaurant →"
                : "Waiting for everyone's card…"}
            </Button>
          </motion.div>
        </div>
      )}

      {/* Address picker sheet */}
      <Sheet open={addressSheetOpen} onOpenChange={setAddressSheetOpen}>
        <SheetContent side="bottom" className="max-h-[70vh] overflow-y-auto">
          <SheetHeader>
            <SheetTitle className="font-display text-xl">Delivery address</SheetTitle>
          </SheetHeader>
          {loadingAddresses ? (
            <p className="font-sans text-sm text-body-muted py-6 text-center">Loading…</p>
          ) : (
            <ul className="flex flex-col divide-y divide-hairline mt-4">
              {addresses.map((addr) => (
                <li key={addr.id}>
                  <button
                    onClick={() => pickAddress(addr)}
                    className="w-full text-left py-3 px-1 hover:bg-canvas-soft transition-colors"
                  >
                    <p className="font-sans font-bold text-sm text-ink">{addr.label}</p>
                    <p className="font-sans text-xs text-body-muted mt-0.5 line-clamp-1">{addr.address}</p>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}
