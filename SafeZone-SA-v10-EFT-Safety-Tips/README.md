# SafeZone SA v8 — Manual EFT Membership

SafeZone SA now uses a simple manual EFT membership model while the service is getting started.

## Membership
- R99 per 30 days.
- No online card gateway is required.
- User pays by EFT and sends proof of payment to management.
- Management records the EFT in the Admin Centre.
- Recording payment activates access for 30 days.
- The server automatically expires access when the 30-day period ends.
- Management can turn access on/off manually, but Access ON will not override an expired paid-until date; a new payment must be recorded.
- Payment history and EFT references are stored in the app.
- Emergency numbers and the emergency page remain accessible without membership.

## Deploy
Upload this ZIP to the GitHub repository, wait for the GitHub Action to finish, then deploy the latest commit in Render.

No Paystack environment variables are required for v8.


## SafeZone EFT payment details
- Account name: SafeZone
- Bank: Capitec
- Account number: 1640233618
- Reference: Client name and surname
- Amount: R99 per 30 days
