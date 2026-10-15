# Employee Attendance & Analytics API

A FastAPI application backed by MongoDB for employee management, attendance tracking, and analytics.

## Prerequisites

- Python 3.11 or later
- MongoDB running locally or a MongoDB connection URI

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Create a `.env` file in the project root:

   ```dotenv
   MONGO_URI=mongodb://127.0.0.1:27017
   MONGO_DB=attendance_db
   ```

4. Start the API from the project root:

   ```bash
   uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

## Verify the API

- Health check: `http://127.0.0.1:8000/health`
- Swagger UI: `http://127.0.0.1:8000/docs`

## Project Documentation

- `openapi.yaml` — API contract
- `DATA_MODEL.md` — data model and schema
- `REVIEW.md` — implementation review and testing notes
- `DECISIONS.md` — technical decisions and trade-offs

Configure MongoDB before starting the application. Keep `.env` and other credentials out of version control.
