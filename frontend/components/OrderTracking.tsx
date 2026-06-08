"use client";

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import confetti from "canvas-confetti";
import { supabase } from "@/lib/supabase";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const STEPS = ["Accepted", "Preparing", "Picked up", "Delivered"];

const STATUS_MESSAGES: Record<string, string> = {
  Accepted: "Your order has been accepted.",
  Preparing: "Your order is being prepared.",
  "Picked up": "Out for delivery.",
  Delivered: "Order delivered. Enjoy!",
  Cancelled: "Order was cancelled.",
  placed: "Order placed — waiting for restaurant.",
};

type OrderRow = {
  id: string;
  swiggy_order_id: string | null;
  restaurant_name: string;
  status: string;
  placed_at: string | null;
  eta_mins: number | null;
};

type SplitRow = {
  id: string;
  participant_id: string;
  display_name: string;
  amount: number;
  paid: boolean;
};

function stepIndex(status: string): number {
  const normalized = status.toLowerCase().replace(/\s+/g, " ").trim();
  return STEPS.findIndex((s) => s.toLowerCase() === normalized);
}

function StepperNode({
  label,
  idx,
  currentIdx,
}: {
  label: string;
  idx: number;
  currentIdx: number;
}) {
  const completed = idx < currentIdx;
  const active = idx === currentIdx;
  const upcoming = idx > currentIdx;

  return (
    <div className="flex flex-col items-center gap-1 flex-1">
      {active ? (
        <motion.div
          className="w-4 h-4 border-2 border-ink bg-canvas"
          animate={{ scale: [1, 1.4, 1] }}
          transition={{ repeat: Infinity, duration: 1.5, ease: "easeInOut" }}
        />
      ) : (
        <div
          className={`w-4 h-4 border ${
            completed ? "bg-ink border-ink" : "bg-hairline border-hairline"
          }`}
        />
      )}
      <span
        className={`text-[10px] font-sans text-center leading-tight ${
          completed || active ? "text-ink" : "text-body-muted"
        }`}
      >
        {label}
      </span>
    </div>
  );
}

function OrderTracker({
  order,
  roomId,
  isHost,
  splits,
  onSplitPaid,
}: {
  order: OrderRow;
  roomId: string;
  isHost: boolean;
  splits: SplitRow[];
  onSplitPaid: (splitId: string) => void;
}) {
  const [status, setStatus] = useState(order.status);
  const [etaMins, setEtaMins] = useState<number | null>(order.eta_mins);
  const [deliveredBanner, setDeliveredBanner] = useState(
    order.status.toLowerCase() === "delivered"
  );
  // Guards the one-shot confetti without forcing the subscription effect to
  // re-run (and tear down / re-open the channel) on every tracking update.
  const firedConfetti = useRef(order.status.toLowerCase() === "delivered");

  // Subscribe to placed_orders changes for this order. Depends only on order.id
  // so the channel is opened once and survives every incoming update — listing
  // status/eta/banner here would resubscribe on each event and drop messages
  // during the resubscribe window.
  useEffect(() => {
    const channel = supabase
      .channel(`order-${order.id}`)
      .on(
        "postgres_changes",
        {
          event: "UPDATE",
          schema: "public",
          table: "placed_orders",
          filter: `id=eq.${order.id}`,
        },
        (payload) => {
          const updated = payload.new as {
            status: string;
            sub_order_data: Record<string, unknown>;
          };
          const newEta =
            typeof updated.sub_order_data?.eta_mins === "number"
              ? (updated.sub_order_data.eta_mins as number)
              : null;

          if (updated.status) setStatus(updated.status);
          if (newEta !== null) setEtaMins(newEta);

          if (updated.status?.toLowerCase() === "delivered" && !firedConfetti.current) {
            firedConfetti.current = true;
            setDeliveredBanner(true);
            confetti({ particleCount: 150, spread: 80, origin: { y: 0.6 } });
          }
        }
      )
      .subscribe();
    return () => {
      supabase.removeChannel(channel);
    };
  }, [order.id]);

  const currentStep = stepIndex(status);
  const isCancelled = status.toLowerCase() === "cancelled";
  const isDelivered = status.toLowerCase() === "delivered";

  return (
    <div className="border border-hairline">
      {/* Order header */}
      <div className="flex items-baseline justify-between px-4 py-3 border-b border-hairline">
        <div>
          <p className="font-display text-lg text-ink leading-tight">
            {order.restaurant_name}
          </p>
          {order.swiggy_order_id && (
            <p className="font-sans text-xs text-body-muted mt-0.5">
              Order #{order.swiggy_order_id}
            </p>
          )}
        </div>
        {etaMins != null && !isDelivered && !isCancelled && (
          <AnimatePresence mode="wait">
            <motion.span
              key={etaMins}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              className="font-sans text-sm font-bold text-ink"
            >
              ~{etaMins} min
            </motion.span>
          </AnimatePresence>
        )}
      </div>

      {/* Delivered banner */}
      <AnimatePresence>
        {deliveredBanner && (
          <motion.div
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            className="bg-ink text-canvas px-4 py-3 flex items-center justify-center"
          >
            <span className="font-display text-base">
              Order delivered. Enjoy.
            </span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Cancelled notice */}
      {isCancelled && (
        <div className="border-t border-hairline px-4 py-3 bg-canvas">
          <p className="font-sans text-sm text-ink font-bold">Order cancelled</p>
          <p className="font-sans text-xs text-body-muted mt-0.5">
            Need help? Call Swiggy care: 080-67466729
          </p>
        </div>
      )}

      {/* Stepper */}
      {!isCancelled && (
        <div className="px-4 py-4">
          <div className="flex items-start relative">
            {/* Connecting line */}
            <div className="absolute top-2 left-6 right-6 h-px bg-hairline -z-0" />
            {STEPS.map((step, i) => (
              <StepperNode
                key={step}
                label={step}
                idx={i}
                currentIdx={currentStep < 0 ? 0 : currentStep}
              />
            ))}
          </div>

          {/* Status text */}
          <AnimatePresence mode="wait">
            <motion.p
              key={status}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="font-sans text-sm text-body-muted mt-3 text-center"
            >
              {STATUS_MESSAGES[status] ?? status}
            </motion.p>
          </AnimatePresence>
        </div>
      )}

      {/* Bill split */}
      {splits.length > 0 && (
        <div className="border-t border-hairline px-4 py-3">
          <p className="font-sans font-bold text-xs text-ink uppercase tracking-wide mb-2">
            Split — mark as received
          </p>
          <div className="flex flex-col divide-y divide-hairline">
            {splits.map((split) => (
              <div
                key={split.id}
                className="flex items-center justify-between py-2"
              >
                <div className="flex items-baseline gap-3">
                  <span className="font-sans text-sm text-ink">
                    {split.display_name}
                  </span>
                  <span className="font-sans text-sm text-body-muted">
                    ₹{split.amount}
                  </span>
                </div>
                {isHost && (
                  <button
                    onClick={() => onSplitPaid(split.id)}
                    className={`w-5 h-5 border flex items-center justify-center text-[11px] font-sans transition-colors ${
                      split.paid
                        ? "bg-ink border-ink text-canvas"
                        : "border-ink text-body-muted hover:bg-canvas-soft"
                    }`}
                    title={split.paid ? "Mark unpaid" : "Mark paid"}
                  >
                    {split.paid ? "✓" : "○"}
                  </button>
                )}
                {!isHost && split.paid && (
                  <span className="font-sans text-xs text-ink">✓</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function OrderTracking({
  roomId,
  isHost,
  hostParticipantId,
  nameByPid,
}: {
  roomId: string;
  isHost: boolean;
  hostParticipantId: string;
  nameByPid: Record<string, string>;
}) {
  const [orders, setOrders] = useState<OrderRow[]>([]);
  const [splits, setSplits] = useState<SplitRow[]>([]);
  const [authExpired, setAuthExpired] = useState(false);

  useEffect(() => {
    fetch(`${API_URL}/rooms/${roomId}/placed-orders`)
      .then((r) => r.json())
      .then((d) => setOrders(d.placed_orders ?? []))
      .catch(() => {});

    fetch(`${API_URL}/rooms/${roomId}/splits`)
      .then((r) => r.json())
      .then((d) => setSplits(d.splits ?? []))
      .catch(() => {});
  }, [roomId]);

  // Listen for auth:expired broadcast
  useEffect(() => {
    const channel = supabase
      .channel(`auth-${roomId}`)
      .on("broadcast", { event: "auth:expired" }, () => setAuthExpired(true))
      .subscribe();
    return () => {
      supabase.removeChannel(channel);
    };
  }, [roomId]);

  async function markSplitPaid(splitId: string) {
    // Send the explicit target state (not a blind toggle) so a retry or
    // double-tap is idempotent and can't flip a paid split back to unpaid.
    const current = splits.find((s) => s.id === splitId);
    const target = !(current?.paid ?? false);
    // Optimistic update
    setSplits((prev) =>
      prev.map((s) => (s.id === splitId ? { ...s, paid: target } : s))
    );
    await fetch(`${API_URL}/rooms/${roomId}/splits/${splitId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ paid: target }),
    }).catch(() => {});
    // Re-fetch to sync
    const res = await fetch(`${API_URL}/rooms/${roomId}/splits`).catch(() => null);
    if (res?.ok) {
      const d = await res.json();
      setSplits(d.splits ?? []);
    }
  }

  if (authExpired) {
    return (
      <div className="w-full max-w-2xl mx-auto px-4 py-12 flex flex-col items-center gap-4">
        <p className="font-display text-xl text-ink text-center">
          Swiggy session expired
        </p>
        <p className="font-sans text-sm text-body-muted text-center">
          The host must reconnect their Swiggy account to continue tracking.
        </p>
        {isHost && (
          <a
            href={`${API_URL}/auth/start?room_id=${roomId}`}
            className="inline-flex items-center px-6 py-2 bg-ink text-canvas font-sans font-bold text-sm"
          >
            Reconnect Swiggy →
          </a>
        )}
      </div>
    );
  }

  if (orders.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-4 py-16">
        <motion.div
          className="w-10 h-10 border border-ink"
          animate={{ rotate: 360 }}
          transition={{ repeat: Infinity, duration: 2.5, ease: "linear" }}
        />
        <p className="font-sans text-sm text-body-muted">Loading order…</p>
      </div>
    );
  }

  return (
    <div className="w-full max-w-2xl mx-auto px-4 py-8 flex flex-col gap-6">
      {orders.map((order) => (
        <OrderTracker
          key={order.id}
          order={order}
          roomId={roomId}
          isHost={isHost}
          splits={splits}
          onSplitPaid={markSplitPaid}
        />
      ))}
    </div>
  );
}
