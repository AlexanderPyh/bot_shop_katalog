# Eurocascade Curtain Studio Telegram Bot

This project is a Telegram-based e-commerce bot system studio. The system consists of two bots: a **User Bot** for customers to browse products, manage their cart, and interact with promotions, and an **Admin Bot** for managing the store's catalog, promotions, and analytics.

## Features

### User Bot (`user_bot.py`)
- **Catalog Navigation**: Browse products by categories, view detailed product information, and add items to the cart.
- **Cart Management**: View, clear, and apply promo codes to items in the cart.
- **Promotions**: View active promotions with descriptions and images.
- **Support Requests**: Submit support queries directly through the bot.
- **Promo Codes**: Apply promo codes to eligible products for discounts.
- **Channel Integration**: Automatically handles join requests for the studio's Telegram channel with welcome messages.
- **Admin Controls**: Admins can toggle keyboard visibility for non-admin users.

### Admin Bot (`admin_bot.py`)
- **Catalog Management**: Add, edit, or delete product categories and products, including details like name, description, price, size, material, and photos.
- **Promotion Management**: Create, view, and delete promotions with start/end dates and optional images.
- **Promo Code Management**: Create, list, and deactivate promo codes with product-specific discounts and validity periods.
- **Support Request Handling**: View and respond to user support requests, with options to block users or delete requests.
- **Mailing System**: Schedule and send mass messages to users with customizable content and timing.
- **Analytics**: Fetch sales data, top products, and user activity, and export metrics to Google Sheets for further analysis.

## Architecture
- **Database**: SQLite (`bot.db`) stores categories, products, user profiles, cart data, promotions, promo codes, support requests, and mailing schedules.
- **Media Storage**: Product photos are stored in a `media` directory.
- **Google Sheets Integration**: Analytics data is exported to Google Sheets using the Google Sheets API.
- **Environment Variables**: Configured via a `.env` file for bot tokens, Google Sheets credentials, and database paths.

## Setup Instructions

### Prerequisites
- Python 3.8+
- Telegram Bot Tokens (for User and Admin bots)
- Google Cloud Service Account credentials for Google Sheets API
- SQLite database (created automatically on first run)

### Installation
1. **Clone the Repository**:
   ```bash
   git clone <repository-url>
   cd eurocascade-bot
   ```

2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
   Ensure the following packages are included in `requirements.txt`:
   ```
   python-telegram-bot==20.7
   python-dotenv==1.0.0
   google-auth==2.23.0
   google-auth-oauthlib==1.0.0
   google-api-python-client==2.100.0
   ```

3. **Set Up Environment Variables**:
   Create a `.env` file in the project root with the following variables:
   ```
   USER_BOT_TOKEN=<your-user-bot-token>
   ADMIN_BOT_TOKEN=<your-admin-bot-token>
   ADMIN_IDS=<comma-separated-admin-telegram-ids>
   GOOGLE_CREDENTIALS_JSON=<path-to-google-credentials.json>
   GSHEET_ANALYTICS_ID=<google-sheets-id>
   DB_PATH=bot.db
   MEDIA_DIR=media
   ```

4. **Prepare Google Sheets**:
   - Create a Google Sheet with sheets named `Sales` and `TopProducts`.
   - Share the sheet with the service account email from your Google Cloud credentials.

5. **Run the Bots**:
   - Start the User Bot:
     ```bash
     python user_bot.py
     ```
   - Start the Admin Bot:
     ```bash
     python admin_bot.py
     ```

## Usage
- **User Bot**: Users interact via `/start` to access the main menu, browse the catalog, manage their cart, apply promo codes, or submit support requests.
- **Admin Bot**: Admins use `/start` to access the admin panel, where they can manage products, promotions, promo codes, support requests, and schedule mailings. Analytics are accessible via the `/analytics` command.

## Database Schema
The SQLite database includes the following tables:
- `categories`: Stores product categories.
- `all_info`: Stores product details (name, description, price, size, material, photo path).
- `buy`: Tracks user cart items.
- `promotions`: Stores promotion details (name, description, image URL, start/end dates).
- `promo_codes`: Stores promo code details (code, product ID, discount percentage, start/end dates).
- `telegram_profiles`: Stores user profiles (Telegram ID, username).
- `support_requests`: Stores user support requests.
- `blocked_users`: Tracks blocked users.
- `join_requests`: Logs channel join requests.
- `settings`: Stores bot settings (e.g., keyboard restriction).
- `mailings`: Stores scheduled mailing details.

## Logging
Both bots log activities to:
- `user_bot.log` for the User Bot.
- `admin_bot.log` for the Admin Bot.
Logs include debug, info, warning, and error messages for troubleshooting.

## Error Handling
- The bots handle Telegram API errors, database errors, and invalid inputs gracefully.
- Conflicts (e.g., multiple bot instances) are detected and logged.
- Users receive clear error messages for invalid actions.

## Notes
- Ensure only one instance of each bot runs at a time to avoid Telegram API conflicts.
- The media directory (`MEDIA_DIR`) must have write permissions for storing product photos.
- Google Sheets credentials must have the correct scopes (`https://www.googleapis.com/auth/spreadsheets`).
- The User Bot requires the bot to have admin permissions in the Telegram channel for join request handling.
