"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function Home() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleCreate() {
    if (!name.trim()) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${API_URL}/rooms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ display_name: name.trim() }),
      });
      if (res.status === 401) {
        window.location.href = `${API_URL}/auth/start`;
        return;
      }
      if (!res.ok) throw new Error("Failed to create room");
      const data = await res.json();
      router.push(`/room/${data.room_id}`);
    } catch {
      setError("Something went wrong. Try again.");
      setLoading(false);
    }
  }

  return (
    <main className="flex flex-1 flex-col items-center justify-center min-h-screen bg-canvas px-6">
      <p className="text-[11px] font-sans font-bold tracking-[0.4px] uppercase text-body-muted mb-8">
        Powered by Swiggy
      </p>

      <h1
        className="font-display text-center text-ink leading-none tracking-tight mb-4"
        style={{ fontSize: "clamp(40px, 7vw, 64px)", letterSpacing: "-0.5px" }}
      >
        Dinner,<br />minus the debate.
      </h1>

      <p
        className="font-body text-center text-body-muted mb-10 max-w-md"
        style={{ fontSize: "19px", lineHeight: "27.93px" }}
      >
        Circle finds the one restaurant (or two) that works for everyone in
        your group — allergies, budgets, and cravings included.
      </p>

      <div className="w-16 border-t border-hairline mb-10" />

      <div className="flex gap-3">
        <Button
          onClick={() => setOpen(true)}
          className="bg-ink text-canvas hover:bg-ink/80 font-sans font-bold text-sm tracking-[0.3px] px-5 py-3 h-auto"
        >
          Start a circle
        </Button>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="font-display text-xl">
              Who&apos;s at the table?
            </DialogTitle>
          </DialogHeader>
          <p className="text-sm text-body-muted font-sans -mt-1">
            Your friends will see this name in the circle.
          </p>
          <input
            placeholder="First name's fine"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleCreate()}
            autoFocus
            className="mt-1 w-full h-12 rounded-2xl border border-hairline bg-canvas-soft px-4 text-base font-sans text-ink placeholder:text-body-muted outline-none transition-colors focus:border-ink focus:bg-canvas"
          />
          {error && <p className="text-sm text-ink font-bold font-sans">{error}</p>}
          <Button
            onClick={handleCreate}
            disabled={!name.trim() || loading}
            className="w-full h-12 rounded-2xl bg-ink text-canvas hover:bg-ink/80 font-sans font-bold"
          >
            {loading ? "Creating…" : "Create circle"}
          </Button>
        </DialogContent>
      </Dialog>
    </main>
  );
}
