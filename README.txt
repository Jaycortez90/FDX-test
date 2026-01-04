Driver Portal (public HTTPS, geofence 30 km around QAR Duiven)

1) Deploy on Render as a Web Service (Python).
   - Build: pip install -r requirements.txt
   - Start: uvicorn main:app --host 0.0.0.0 --port $PORT

2) Set Render environment variables:
   R2_ACCOUNT_ID
   R2_ACCESS_KEY_ID
   R2_SECRET_ACCESS_KEY
   R2_BUCKET
   SNAPSHOT_KEY=status.json
   ADMIN_UPLOAD_SECRET=<random long secret>

3) After deploy, open the public URL:
   https://<your-service>.onrender.com/
   Drivers enter plate and allow GPS.

API behavior:
- Access only within 30.0 km of QAR Duiven (51.9672245, 6.0205411)
- Plate must match exactly ONE movement; 0 -> 404, multiple -> 409


Quick test without R2:
1) Deploy service (no R2 env vars needed).
2) Set only: ADMIN_UPLOAD_SECRET
3) Upload a snapshot:
   POST https://<service>.onrender.com/api/upload?secret=<ADMIN_UPLOAD_SECRET>
   Body JSON: {"last_update":"...","movements":[...]}
4) Open:
   https://<service>.onrender.com/
