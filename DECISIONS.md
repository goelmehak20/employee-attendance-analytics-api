# Decisions

- Added indexes to avoid duplicate employees and attendance records and improve queries.
- Duplicate punch-ins return `409 Conflict`.
- Used MongoDB `$rank` to handle ties in the late leaderboard.
- Department summary includes joined employees even if they have no attendance records.
- For larger data, I would check query performance and consider pagination or precomputed analytics.