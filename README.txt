Driver Portal (Public) - QAR Geofence 30km

Status rules shown (big text)
- If CloseDoor has value but Location empty:
  "Your trailer is ready, please report in the office for further information!"
- If Location has value:
  "Please connect the (Trailer) trailer on location: (Location) and pick up the CMR documents in the office!"
- If no CloseDoor and >45 minutes left until Scheduled departure:
  "Your trailer being loaded, please wait!"
- If no CloseDoor and <=45 minutes left until Scheduled departure:
  "Please report in the office!"

Displayed fields
- Destination: "City, Land (CODE)" (clickable => opens navigation by coordinates)
- Scheduled departure date/time
- Report in the office: Scheduled departure - 45 minutes
- Last refresh

Desktop snapshot upload
- POST /api/upload?secret=ADMIN_UPLOAD_SECRET
- Snapshot JSON must contain:
  { "last_update": "...", "movements": [ { license_plate, destination_text, destination_lat, destination_lon,
                                          scheduled_departure, close_door, location, trailer, ... } ] }

Environment variables (Render)
Required:
- ADMIN_UPLOAD_SECRET

Optional Push Notifications
Requires:
- VAPID_PUBLIC_KEY
- VAPID_PRIVATE_KEY
- VAPID_SUBJECT (example: mailto:you@example.com)

Generate VAPID keypair locally:
  python -c "from pywebpush import generate_vapid_keypair; print(generate_vapid_keypair())"

Limitations
- Subscriptions are stored in memory; Render restarts will clear them.

Push note:
- The server also re-checks statuses every 60s, so the 45-minute threshold can trigger push without new uploads.
