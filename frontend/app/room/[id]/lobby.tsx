"use client";

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { QRCodeSVG } from "qrcode.react";
import { toast, Toaster } from "sonner";
import { Copy, ExternalLink, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { supabase } from "@/lib/supabase";

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

export default function Lobby({
  initialRoom,
  roomId,
}: {
  initialRoom: RoomState;
  roomId: string;
}) {
  const [participants, setParticipants] = useState<Participant[]>(
    initialRoom.participants
  );
  const [addressSheetOpen, setAddressSheetOpen] = useState(false);
  const [addresses, setAddresses] = useState<Address[]>([]);
  const [selectedAddress, setSelectedAddress] = useState<Address | null>(null);
  const [loadingAddresses, setLoadingAddresses] = useState(false);
  const participantsRef = useRef(participants);
  participantsRef.current = participants;

  const joinUrl = `${typeof window !== "undefined" ? window.location.origin : ""}/join/${roomId}`;
  const cardsSubmitted = participants.filter((p) => p.has_card).length;
  const allSubmitted = participants.length > 0 && cardsSubmitted === participants.length;

  // Supabase Realtime
  useEffect(() => {
    const channel = supabase
      .channel(`room-${roomId}`)
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "participants", filter: `room_id=eq.${roomId}` },
        (payload) => {
          const p = payload.new as Participant;
          setParticipants((prev) =>
            prev.find((x) => x.id === p.id)
              ? prev
              : [...prev, { ...p, has_card: false }]
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
            prev.map((p) =>
              p.id === card.participant_id ? { ...p, has_card: true } : p
            )
          );
          const who = participantsRef.current.find(
            (p) => p.id === card.participant_id
          );
          if (who) toast(`${who.display_name} submitted their card`);
        }
      )
      .subscribe();
    return () => { supabase.removeChannel(channel); };
  }, [roomId]);

  async function openAddressPicker() {
    setAddressSheetOpen(true);
    if (addresses.length) return;
    setLoadingAddresses(true);
    try {
      const res = await fetch(`${API_URL}/rooms/${roomId}/addresses`, {
        credentials: "include",
      });
      if (!res.ok) throw new Error();
      const data = await res.json();
      // Parse the MCP text response into address objects
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

  function copyLink() {
    navigator.clipboard.writeText(joinUrl);
    toast("Link copied");
  }

  return (
    <div className="min-h-screen bg-canvas flex flex-col">
      <Toaster position="top-right" />

      {/* Header */}
      <header className="flex items-center justify-between px-6 py-4 border-b border-hairline">
        <span className="font-sans font-bold text-sm tracking-[0.3px] text-ink">
          Circle
        </span>
        <button
          onClick={openAddressPicker}
          className="flex items-center gap-1.5 text-sm font-sans text-body-muted hover:text-ink transition-colors"
        >
          {selectedAddress ? (
            <span className="border border-hairline px-2 py-0.5 text-xs font-sans">
              {selectedAddress.label}
            </span>
          ) : (
            <span className="text-xs font-sans text-body-muted">Set delivery address</span>
          )}
          <ExternalLink size={12} />
        </button>
      </header>

      {/* Body */}
      <div className="flex flex-1 flex-col md:flex-row divide-y md:divide-y-0 md:divide-x divide-hairline">
        {/* Left — QR + code */}
        <div className="flex flex-col items-center justify-center gap-6 p-8 md:flex-1">
          <QRCodeSVG value={joinUrl} size={200} />
          <div className="text-center">
            <p className="text-xs font-sans text-body-muted uppercase tracking-widest mb-1">
              Room code
            </p>
            <p className="font-display text-2xl tracking-wider text-ink">
              {initialRoom.room_code}
            </p>
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
                    <span
                      className={`w-2 h-2 rounded-full ${
                        p.has_card ? "bg-ink" : "bg-hairline animate-pulse"
                      }`}
                    />
                    <span className="font-sans text-sm text-ink">
                      {p.display_name}
                      {p.is_host && (
                        <span className="ml-1 text-xs text-body-muted">(you)</span>
                      )}
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

      {/* Footer — Activate */}
      <div className="border-t border-hairline p-4 flex justify-center">
        <motion.div
          animate={allSubmitted ? { scale: [1, 1.03, 1] } : { scale: 1 }}
          transition={allSubmitted ? { repeat: Infinity, duration: 1.8 } : {}}
        >
          <Button
            disabled={!allSubmitted}
            className="bg-ink text-canvas hover:bg-ink/80 font-sans font-bold px-8 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {allSubmitted ? "Activate — find our restaurant →" : "Waiting for everyone's card…"}
          </Button>
        </motion.div>
      </div>

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
                    className="w-full text-left py-3 px-1 hover:bg-zinc-50 transition-colors"
                  >
                    <p className="font-sans font-bold text-sm text-ink">{addr.label}</p>
                    <p className="font-sans text-xs text-body-muted mt-0.5 line-clamp-1">
                      {addr.address}
                    </p>
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
