"""Run-lifecycle notifications, delivered through a transactional outbox.

    engine transition ──(same transaction)──▶ notification_deliveries (pending)
    scheduler job ──claims due rows (lease)──▶ sender ──▶ worker ──▶ Telegram / webhook

Why an outbox? Sending inside the engine's transaction would either block the
transaction on the network or risk "run failed but nobody was told" when the send
fails after commit. Writing the intent in the *same* transaction as the state change
means a notification exists if and only if the transition happened.
"""
