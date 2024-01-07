from datetime import datetime

import sunpy.timeseries as ts
from astropy import units as u
from sunpy.net import Fido, attrs as a
from sunpy.time import TimeRange


def hro2_1min(starttime: datetime, endtime: datetime):
    """
    meant as a drop in replacement for heliopy's hro2_1min
    """
    time_range = TimeRange(starttime, endtime)
    result = Fido.search(
        a.Time(time_range.start, time_range.end),
        a.Instrument('OMNI2'),
        a.Physobs('HRO2'),
        a.Sample(u.minute)
    )

    downloaded_files = Fido.fetch(result)
    timeseries = ts.TimeSeries(downloaded_files)
    monthly_timeseries = timeseries.resample('1M')
    return monthly_timeseries
