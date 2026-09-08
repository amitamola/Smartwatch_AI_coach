# Weekly review

Start `AgBot - Weekly Review - <TODAY>`. Apply the shared coaching policy.
Summarize actual training, recovery, capability changes and relevant longer-term
metrics against the user's goals. Distinguish commuting, recovery and intentional
sessions using the supplied classifications; report coverage limitations.

Review what progressed, what was too difficult, and what remains unknown. Do not
claim all weekly muscle volume is known when some workouts lack set detail.
Use TRAINING_STATE to propose the next week's training and recovery slots within
the user's availability. Emit dated SESSION_PLAN markers for those proposals.
Do not silently turn proposals into completed workouts or override exclusions.

Use the saved PROGRAMME_STATE rather than inventing a separate competing programme.
Explain where observed performance and current feedback support retaining,
progressing or changing its exercises. Preserve its recovery budget and explain
any requested or safety-driven deviations in the dated plan metadata.
