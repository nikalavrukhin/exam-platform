# Exam Submission Platform

Web app for running coding quizzes: students enter their name and paste solutions; an admin reviews submissions behind a password-protected panel. Deployed in production on a self-hosted Linux server.

**Stack:** vanilla JS single-page frontend, Python 3 standard-library backend, SQLite, nginx reverse proxy, Docker.

**Features**
- Student submissions with per-student secret tokens (resume/edit your own work)
- Admin panel with token-based auth, expiry and brute-force protection
- Python syntax checking of submitted code on the server
- Zero third-party dependencies

**Run**
```bash
ADMIN_PASSWORD=your-password python3 server.py
```
