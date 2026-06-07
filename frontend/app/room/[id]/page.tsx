import { notFound } from "next/navigation";
import Lobby from "./lobby";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default async function RoomPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const res = await fetch(`${API_URL}/rooms/${id}`, { cache: "no-store" });
  if (res.status === 404) notFound();
  const room = await res.json();
  return <Lobby initialRoom={room} roomId={id} />;
}
