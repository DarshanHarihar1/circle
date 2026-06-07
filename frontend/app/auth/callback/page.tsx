import CallbackClient from "./client";

export default async function AuthCallbackPage({
  searchParams,
}: {
  searchParams: Promise<{ room_id?: string }>;
}) {
  const { room_id } = await searchParams;
  return <CallbackClient roomId={room_id ?? null} />;
}
