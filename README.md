# PayrollX — Payroll Management System

A production-ready, multi-tenant payroll management system with JWT authentication, Role-Based Access Control (RBAC), and server-side PDF payslip generation. Built for Indian businesses with full statutory compliance (EPF, ESI, PT, TDS).

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.1-green?logo=flask&logoColor=white)
![JWT](https://img.shields.io/badge/Auth-JWT+Session-orange?logo=jsonwebtokens&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-purple)

---

## Features

### 🔐 Authentication & Security
- **Dual-entry login** — Organization Admin vs Employee (with Employee ID)
- **JWT API tokens** — Stateless auth for programmatic access
- **Session-based auth** — Flask-Login for browser users
- **RBAC** — hr_admin, finance, employee roles
- **Row-Level Security** — Every query scoped by `org_id` + `user_id`
- **IDOR Prevention** — Employees can only access their own data
- **Audit Trail** — All write operations logged automatically

### 💰 Payroll Engine
- **Attendance-based pay** — Salary proportional to days worked
- **Statutory compliance** — EPF (12%), ESI (0.75%), Professional Tax, TDS
- **New/Old tax regime** — FY 2025-26 slabs with 4% cess
- **LOP deduction** — Loss of Pay on top of attendance adjustment
- **Payroll lifecycle** — Draft → Pending → Finalized

### 📄 Reports & Documents
- **PDF Payslips** — Server-side generation with ReportLab
- **6 Report types** — Payroll Summary, Statutory Compliance, Department Cost, YTD, Bank Transfer, Audit Trail
- **Reimbursements** — Employee expense claim tracking
- **Tax Declarations** — Section 80C, 80D investment tracking

### 🏢 Multi-Tenant Architecture
- **Organization isolation** — `org_id` on every table
- **No cross-org data leakage** — Enforced at database query level
- **Self-only policy** — Employees see only their own records

---

## Quick Start

### Prerequisites
- Python 3.10+
- pip

### Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/PayrollX.git
cd PayrollX/PayrollManagementSystem/My\ Project

# Install dependencies
pip install -r src/requirements.txt

# Run the application
python launch.py
```

The app will start at `http://127.0.0.1:5000` and auto-open your browser.
The database is auto-created and seeded with demo data on first run.

### Demo Credentials

| Mode | Username | Password | Role |
|------|----------|----------|------|
| Admin | `admin` | `admin123` | HR Admin |
| Admin | `finance` | `finance123` | Finance |
| Employee | `EMP1` | `rahul123` | Employee |
| Employee | `EMP2` | `priya123` | Employee |

---

## API Endpoints

### Authentication
```bash
# Get JWT token
curl -X POST http://localhost:5000/api/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}'
```

### Employee Data
```bash
# Get employee data (with JWT)
curl http://localhost:5000/api/v1/employee/data?emp_id=1 \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### PDF Receipt
```bash
# Generate PDF payslip
curl http://localhost:5000/api/v1/receipt/generate?slip_id=1 \
  -H "Authorization: Bearer YOUR_TOKEN" \
  --output payslip.pdf
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Flask 3.1, Python 3.10+ |
| Database | SQLite (dev) / PostgreSQL (prod) |
| Auth | Flask-Login (sessions) + PyJWT (API) |
| PDF | ReportLab |
| WSGI | Waitress |
| Frontend | Jinja2, Vanilla CSS, Font Awesome |

---

## Project Structure

```
PayrollManagementSystem/
├── My Project/
│   ├── launch.py              # Application launcher (Waitress WSGI)
│   ├── src/
│   │   ├── app.py             # Flask routes, controllers, payroll engine
│   │   ├── models.py          # SQLAlchemy models (11 tables)
│   │   ├── middleware.py       # JWT engine, auth decorators, PDF generator
│   │   ├── config.py          # Configuration (JWT, DB, paths)
│   │   └── requirements.txt   # Python dependencies
│   ├── templates/             # 27 Jinja2 HTML templates
│   └── static/
│       ├── css/style.css      # Design system
│       └── js/main.js         # Client-side interactions
└── .gitignore
```

---

## Security Architecture

```
Browser ──→ Flask-Login Session ──→ Route Handler ──→ DB Query (WHERE org_id = ?)
API Client ──→ JWT Bearer Token ──→ Middleware ──→ Route Handler ──→ DB Query (WHERE org_id = ?)
                                      │
                                      └──→ Audit Logger (POST/PUT/DELETE)
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.
