"""Email watcher self-reply loop guard. [2026-07-24]

The bot's Gmail account is the same one it reads AND sends from, so every Q&A
auto-answer it sends bounces back into the inbox looking 'from Eyal'. Without a
guard the watcher answered its own reply -> a self-reply loop (every ~2h, 26
messages, kicked off by an n8n-course test email). This pins the discriminator:
our replies sign off as 'Gianluigi, CropSight AI Assistant'.
"""
from schedulers.email_watcher import email_watcher


class TestOwnOutboundGuard:
    def test_full_signature_reply_is_ours(self):
        body = ("Hi Eyal,\n\nHey Eyal — looks like the message came through a bit "
                "jumbled. What did you want to ask?\n\n---\nGianluigi, CropSight AI Assistant")
        assert email_watcher._is_own_outbound_message(body) is True

    def test_bare_gianluigi_signoff_is_ours(self):
        assert email_watcher._is_own_outbound_message("Here you go.\n\n---\nGianluigi") is True

    def test_real_human_question_is_not_ours(self):
        assert email_watcher._is_own_outbound_message(
            "Hey — what's the status on the Moldova pilot? Thanks") is False

    def test_empty_body_is_not_ours(self):
        assert email_watcher._is_own_outbound_message("") is False
        assert email_watcher._is_own_outbound_message(None) is False
