"use client";

import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type SplitRow = {
  id: string;
  participant_id: string;
  display_name: string;
  amount: number;
  upi_link: string | null;
  paid: boolean;
};

type SubOrderItem = {
  participant_id: string;
  item_name: string;
  price: number;
};

type SubOrder = {
  restaurant_id: string;
  restaurant_name: string;
  items: SubOrderItem[];
  coupon_code: string | null;
  subtotal: number;
  fees: number;
  discount: number;
  total: number;
};

type Plan = {
  id: string;
  kind: string;
  sub_orders: SubOrder[];
  total: number;
  chosen: boolean;
};

function UpiButton({
  split,
  hostVpa,
  roomId,
}: {
  split: SplitRow;
  hostVpa: string;
  roomId: string;
}) {
  const upiLink =
    split.upi_link ||
    (hostVpa
      ? `upi://pay?pa=${encodeURIComponent(hostVpa)}&am=${split.amount}&tn=${encodeURIComponent("Circle " + roomId.slice(0, 6))}&cu=INR`
      : null);

  if (!upiLink) return null;

  return (
    <a
      href={upiLink}
      className="inline-flex items-center px-3 py-1.5 border border-ink font-sans text-xs text-ink hover:bg-ink hover:text-canvas transition-colors"
    >
      Pay via UPI →
    </a>
  );
}

export default function CartReview({
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
  const [plan, setPlan] = useState<Plan | null>(null);
  const [splits, setSplits] = useState<SplitRow[]>([]);
  const [hostVpa, setHostVpa] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [placing, setPlacing] = useState(false);
  const [subOrderIdx, setSubOrderIdx] = useState(0);

  useEffect(() => {
    fetch(`${API_URL}/rooms/${roomId}/plans`)
      .then((r) => r.json())
      .then((d) => {
        const chosen = (d.plans ?? []).find((p: Plan) => p.chosen);
        if (chosen) setPlan(chosen);
      })
      .catch(() => {});

    fetch(`${API_URL}/rooms/${roomId}/splits`)
      .then((r) => r.json())
      .then((d) => setSplits(d.splits ?? []))
      .catch(() => {});
  }, [roomId]);

  // Re-fetch splits when status returns to confirming (multi-restaurant)
  useEffect(() => {
    const t = setInterval(() => {
      fetch(`${API_URL}/rooms/${roomId}/splits`)
        .then((r) => r.json())
        .then((d) => setSplits(d.splits ?? []))
        .catch(() => {});
    }, 3000);
    return () => clearInterval(t);
  }, [roomId]);

  async function handlePlaceOrder() {
    setPlacing(true);
    setConfirmOpen(false);
    try {
      await fetch(`${API_URL}/rooms/${roomId}/confirm-order`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ host_vpa: hostVpa || null }),
      });
      setSubOrderIdx((n) => n + 1);
    } catch {
      /* silent — status polling will reflect the change */
    } finally {
      setPlacing(false);
    }
  }

  if (!plan) {
    return (
      <div className="flex flex-col items-center justify-center gap-4 py-20">
        <motion.div
          className="w-10 h-10 border border-ink"
          animate={{ rotate: 360 }}
          transition={{ repeat: Infinity, duration: 2.5, ease: "linear" }}
        />
        <p className="font-sans text-sm text-body-muted">Building your cart…</p>
      </div>
    );
  }

  const currentSub = plan.sub_orders[subOrderIdx] ?? plan.sub_orders[0];
  if (!currentSub) return null;

  const itemsByPid: Record<string, SubOrderItem[]> = {};
  for (const item of currentSub.items) {
    if (!itemsByPid[item.participant_id]) itemsByPid[item.participant_id] = [];
    itemsByPid[item.participant_id].push(item);
  }

  const totalOrders = plan.sub_orders.length;
  const isMulti = plan.kind === "multi";

  return (
    <div className="w-full max-w-2xl mx-auto px-4 py-8 flex flex-col gap-6">
      {/* Heading */}
      <div>
        <p className="font-sans text-xs uppercase tracking-[0.4px] text-body-muted mb-1">
          {isMulti ? `Order ${subOrderIdx + 1} of ${totalOrders}` : "Your order"}
        </p>
        <h1 className="font-display text-3xl text-ink leading-tight">
          {currentSub.restaurant_name}
        </h1>
      </div>

      {/* Per-participant items */}
      <div className="flex flex-col divide-y divide-hairline border border-hairline">
        {Object.entries(itemsByPid).map(([pid, items]) => {
          const name = nameByPid[pid] ?? "Guest";
          const personTotal = items.reduce((s, i) => s + i.price, 0);
          return (
            <div key={pid} className="px-4 py-3">
              <div className="flex items-baseline justify-between mb-1.5">
                <span className="font-sans font-bold text-sm text-ink">{name}</span>
                <span className="font-sans text-sm text-body-muted">₹{personTotal}</span>
              </div>
              {items.map((item, i) => (
                <div key={i} className="flex justify-between pl-3">
                  <span className="font-sans text-xs text-body-muted">
                    {item.item_name} (1×)
                  </span>
                  <span className="font-sans text-xs text-body-muted">₹{item.price}</span>
                </div>
              ))}
            </div>
          );
        })}
      </div>

      {/* Totals */}
      <div className="border-t border-hairline pt-3 flex flex-col gap-1">
        <div className="flex justify-between font-sans text-sm text-body-muted">
          <span>Subtotal</span>
          <span>₹{currentSub.subtotal}</span>
        </div>
        <div className="flex justify-between font-sans text-sm text-body-muted">
          <span>Delivery fee</span>
          <span>₹{currentSub.fees}</span>
        </div>
        {currentSub.coupon_code && (
          <div className="flex justify-between font-sans text-sm text-body-muted">
            <span>Coupon ({currentSub.coupon_code})</span>
            <span>−₹{currentSub.discount}</span>
          </div>
        )}
        {currentSub.total >= 1000 && (
          <div className="flex items-center gap-2 border border-ink px-3 py-2 mt-1">
            <span className="font-sans text-xs font-bold text-ink">⚠</span>
            <span className="font-sans text-xs text-ink">
              Cart total ₹{currentSub.total} exceeds ₹1000 limit — some items may not be orderable.
            </span>
          </div>
        )}
        <div className="flex justify-between font-sans font-bold text-sm text-ink border-t border-hairline pt-2 mt-1">
          <span>Total</span>
          <span>₹{currentSub.total}</span>
        </div>
      </div>

      {/* Bill split */}
      {splits.length > 0 && (
        <div className="flex flex-col gap-3">
          <p className="font-sans font-bold text-sm text-ink">Split</p>

          {/* Optional: host VPA input for UPI links */}
          {isHost && (
            <div className="flex items-center gap-2">
              <input
                type="text"
                placeholder="Your UPI ID (for payment links)"
                value={hostVpa}
                onChange={(e) => setHostVpa(e.target.value)}
                className="flex-1 border border-hairline px-3 py-1.5 font-sans text-xs text-ink placeholder-body-muted outline-none focus:border-ink"
              />
            </div>
          )}

          <div className="flex flex-col divide-y divide-hairline border border-hairline">
            {splits.map((split) => (
              <div
                key={split.id}
                className="flex items-center justify-between px-4 py-3"
              >
                <div className="flex items-baseline gap-3">
                  <span className="font-sans text-sm text-ink">{split.display_name}</span>
                  <span className="font-sans font-bold text-sm text-ink">₹{split.amount}</span>
                </div>
                <UpiButton split={split} hostVpa={hostVpa} roomId={roomId} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Place Order — host only */}
      {isHost && (
        <div className="pt-2">
          <Button
            onClick={() => setConfirmOpen(true)}
            disabled={placing}
            className="w-full bg-ink text-canvas hover:bg-ink/80 font-sans font-bold"
          >
            {placing
              ? "Placing order…"
              : isMulti
              ? `Place Order ${subOrderIdx + 1} of ${totalOrders}`
              : "Place Order"}
          </Button>
        </div>
      )}

      {/* Confirm dialog */}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="font-display text-xl">Confirm order</DialogTitle>
            <DialogDescription className="font-sans text-sm text-body-muted">
              Place a COD order for ₹{currentSub.total} at {currentSub.restaurant_name}?
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => setConfirmOpen(false)}
              className="border-ink font-sans text-sm"
            >
              Cancel
            </Button>
            <Button
              onClick={handlePlaceOrder}
              className="bg-ink text-canvas hover:bg-ink/80 font-sans font-bold"
            >
              Place order →
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
