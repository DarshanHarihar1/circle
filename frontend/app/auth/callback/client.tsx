"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function CallbackClient({ roomId }: { roomId: string | null }) {
  const router = useRouter();

  useEffect(() => {
    const id = setTimeout(() => {
      router.push(roomId ? `/room/${roomId}` : "/");
    }, 1500);
    return () => clearTimeout(id);
  }, [roomId, router]);

  return (
    <main className="flex flex-1 flex-col items-center justify-center min-h-screen bg-canvas px-6">
      <p className="text-[11px] font-sans font-bold tracking-[0.4px] uppercase text-body-muted mb-8">
        Circle
      </p>

      <h1
        className="font-display text-center text-ink leading-none tracking-tight mb-4"
        style={{ fontSize: "clamp(28px, 5vw, 44px)" }}
      >
        Swiggy connected.
      </h1>

      <div className="w-16 border-t border-hairline mb-6" />

      <p className="font-sans text-sm text-body-muted">Redirecting…</p>
    </main>
  );
}
