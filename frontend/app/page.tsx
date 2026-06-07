import { Button } from "@/components/ui/button";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function Home() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center min-h-screen bg-canvas px-6">
      {/* Masthead */}
      <p className="text-[11px] font-sans font-bold tracking-[0.4px] uppercase text-body-muted mb-8">
        Powered by Swiggy
      </p>

      {/* Hero headline — Playfair Display, DESIGN.md display-hero */}
      <h1
        className="font-display text-center text-ink leading-none tracking-tight mb-4"
        style={{ fontSize: "clamp(40px, 7vw, 64px)", letterSpacing: "-0.5px" }}
      >
        Group food,<br />decided together.
      </h1>

      <p
        className="font-body text-center text-body-muted mb-10 max-w-md"
        style={{ fontSize: "19px", lineHeight: "27.93px" }}
      >
        Circle finds the one restaurant (or two) that works for everyone in
        your group — allergies, budgets, and cravings included.
      </p>

      {/* Hairline divider */}
      <div className="w-16 border-t border-hairline mb-10" />

      {/* CTAs — button-primary (black fill) and button-outline */}
      <div className="flex gap-3">
        <Button asChild className="bg-ink text-canvas hover:bg-ink/80 font-sans font-bold text-sm tracking-[0.3px] px-5 py-3 h-auto">
          <a href={`${API_URL}/auth/start`}>Start a circle</a>
        </Button>
        <Button
          variant="outline"
          className="bg-canvas text-ink border-ink hover:bg-zinc-50 font-sans font-bold text-sm tracking-[0.3px] px-5 py-3 h-auto"
        >
          How it works
        </Button>
      </div>
    </main>
  );
}
