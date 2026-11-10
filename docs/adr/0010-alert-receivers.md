# 0010. Alert receivers: local webhook sink; real receivers later

Status: accepted 2026-11-10
Context: alerts route by severity (page or ticket/warning). There's no on-call tool yet.
Options:
1. dev and CI: Alertmanager → a webhook sink container (one per severity route), which the alert drills read. uat and prod: placeholder receivers, with URLs from secrets, to be chosen by the user (for example PagerDuty for page, Slack or email for ticket).
2. Mailpit as the receiver: also workable, but harder to assert on than webhook payloads.
Recommendation: option 1.
Decision: the recommendation above, accepted by the user on 2026-11-10 (plan sign-off).
Consequences: `amtool check-config` in CI. Choosing real receivers is open for the user.
