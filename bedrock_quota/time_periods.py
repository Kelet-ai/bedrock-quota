"""Time period calculations for CloudWatch queries."""

from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from .models import TimeRange, TimePeriod


def get_time_range(period: TimePeriod) -> TimeRange:
    """Calculate start/end times for a time period."""
    now = datetime.utcnow()

    if period == TimePeriod.HOURS_24:
        start = now - timedelta(hours=24)
        end = now
        label = "Last 24h"

    elif period == TimePeriod.TODAY:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
        label = "Today"

    elif period == TimePeriod.DAYS_7:
        start = now - timedelta(days=7)
        end = now
        label = "Last 7 Days"

    elif period == TimePeriod.DAYS_14:
        start = now - timedelta(days=14)
        end = now
        label = "Last 14 Days"

    elif period == TimePeriod.DAYS_30:
        start = now - timedelta(days=30)
        end = now
        label = "Last 30 Days"

    elif period == TimePeriod.CURRENT_MONTH:
        # First day of current month to now
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
        label = f"{now.strftime('%B %Y')} (MTD)"

    elif period == TimePeriod.LAST_MONTH:
        # Full last calendar month
        first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        first_of_last_month = first_of_this_month - relativedelta(months=1)
        start = first_of_last_month
        end = first_of_this_month
        label = first_of_last_month.strftime("%B %Y")

    else:
        raise ValueError(f"Unknown time period: {period}")

    return TimeRange(start=start, end=end, label=label)


def get_today_range() -> TimeRange:
    """Get time range for today (current day only)."""
    now = datetime.utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now
    return TimeRange(start=start, end=end, label="Today")
