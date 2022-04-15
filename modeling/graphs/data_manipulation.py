import numpy as np
import xarray as xr
import datetime as dt
import scipy.optimize as optimize
from scipy.stats import binned_statistic

# For debugging purposes
np.set_printoptions(threshold=np.inf)


def cart2polar(pos_cart, factor=1, shift=0):
    '''
    Rotate cartesian position coordinates to polar coordinates
    '''
    r = (np.sqrt(np.sum(pos_cart[:, [0, 1]] ** 2, axis=1)) * factor)
    phi = (np.arctan2(pos_cart[:, 1], pos_cart[:, 0])) + shift
    pos_polar = xr.concat([r, phi], dim='polar').T.assign_coords({'polar': ['r', 'phi']})

    return pos_polar


def rot2polar(vec, pos, dim):
    '''
    Rotate vector from cartesian coordinates to polar coordinates
    '''
    # Polar unit vectors
    phi = pos.loc[:, 'phi']
    r_hat = xr.concat([np.cos(phi).expand_dims(dim), np.sin(phi).expand_dims(dim)], dim=dim)
    phi_hat = xr.concat([-np.sin(phi).expand_dims(dim), np.cos(phi).expand_dims(dim)], dim=dim)

    # Rotate vector to polar coordinates
    Vr = vec[:, [0, 1]].dot(r_hat, dims=dim)
    Vphi = vec[:, [0, 1]].dot(phi_hat, dims=dim)
    v_polar = xr.concat([Vr, Vphi], dim='polar').T.assign_coords({'polar': ['r', 'phi']})

    return v_polar


def remove_spacecraft_efield(edi_data, fgm_data, mec_data):
    # The assumption is that this is done prior to remove_corot_efield
    # This should be done BEFORE converting edi_data to polar

    # E = v x B, 1e-3 converts units to mV/m
    E_sc = 1e-3 * np.cross(mec_data['V_sc'][:, :3], fgm_data['B_GSE'][:, :3])

    # Make into a DataArray to subtract the data easier
    E_sc = xr.DataArray(E_sc,
                        dims=['time', 'E_index'],
                        coords={'time': edi_data['time'],
                                'E_index': ['Ex', 'Ey', 'Ez']},
                        name='E_sc')

    original_egse=edi_data['E_GSE']

    # Remove E_sc from the measured electric field
    edi_data['E_GSE'] = edi_data['E_GSE'] - E_sc

    edi_data['Original_E_GSE'] = original_egse

    edi_data['E_sc'] = E_sc

    return edi_data


def remove_corot_efield(edi_data, mec_data):
    # This should be done after remove_spacecraft_efield
    # This should be done BEFORE converting edi_data to polar

    # E_corot = C_corot*R_E/r^2 * 𝜙_hat
    # C_corot is found here
    omega_E = 2 * np.pi / (24 * 3600)  # angular velocity of Earth (rad/sec)
    B_0 = 3.12e4  # Earth mean field at surface (nT)
    R_E = 6371.2  # Earth radius (km)
    C_corot = omega_E * B_0 * R_E ** 2 * 1e-3  # V (nT -> T 1e-9, km**2 -> (m 1e3)**2)

    # Position in spherical GSE coordinates
    r_sphr = np.linalg.norm(mec_data['R_sc'], ord=2,
                            axis=mec_data['R_sc'].get_axis_num('R_sc_index'))
    phi_sphr = np.arctan2(mec_data['R_sc'][:, 1], mec_data['R_sc'][:, 0])
    theta_sphr = np.arccos(mec_data['R_sc'][:, 2] / r_sphr)

    # Radial position in equatorial plane
    #  - for some reason phi_cyl = np.arcsin(y/r_cyl) did not work
    r_cyl = r_sphr * np.sin(theta_sphr)
    phi_cyl = np.arctan2(mec_data['R_sc'][:, 1], mec_data['R_sc'][:, 0])
    z_cyl = mec_data['R_sc'][:, 2]

    # Corotation Electric Field
    #  - taken from data_manipulation.remove_corot_efield
    #  - Azimuthal component in the equatorial plane (GSE)
    E_corot = (-92100 * R_E / r_cyl ** 2)
    E_corot = np.stack([np.zeros(len(E_corot)), E_corot, np.zeros(len(E_corot))], axis=1)
    E_corot = xr.DataArray(E_corot,
                           dims=['time', 'E_index'],
                           coords={'time': mec_data['time'],
                                   'E_index': ['r', 'phi', 'z']})

    # Unit vectors
    x_hat = np.stack([np.cos(phi_cyl), -np.sin(phi_cyl), np.zeros(len(phi_cyl))], axis=1)
    y_hat = np.stack([np.sin(phi_cyl), np.cos(phi_cyl), np.zeros(len(phi_cyl))], axis=1)

    # Transform to Cartesian
    Ex_corot = np.einsum('ij,ij->i', E_corot, x_hat)
    Ey_corot = np.einsum('ij,ij->i', E_corot, y_hat)
    Ez_corot = np.zeros(len(x_hat))
    E_gse_corot = xr.DataArray(np.stack([Ex_corot, Ey_corot, Ez_corot], axis=1),
                               dims=['time', 'E_index'],
                               coords={'time': E_corot['time'],
                                       'E_index': ['x', 'y', 'z']})

    minus_spacecraft_egse = edi_data['E_GSE'].copy(deep=True)

    # For some reason edi_data['E_GSE'] = edi_data['E_GSE'] - E_gse_corot results in all nan's. strange. This works tho
    edi_data['E_GSE'][:, 0] = edi_data['E_GSE'][:, 0] - E_gse_corot[:, 0]
    edi_data['E_GSE'][:, 1] = edi_data['E_GSE'][:, 1] - E_gse_corot[:, 1]
    edi_data['E_GSE'][:, 2] = edi_data['E_GSE'][:, 2] - E_gse_corot[:, 2]

    edi_data['no_spacecraft_E_GSE'] = minus_spacecraft_egse

    edi_data['E_Corot'] = E_gse_corot

    return edi_data



def slice_data_by_time(full_data, ti, te):
    # Here is where the desired time and data values will be placed
    time = np.array([])
    wanted_value = np.array([])

    # Slice the wanted data and put them into 2 lists
    for counter in range(0, len(full_data)):
        # The data at each index is all in one line, separated by whitespace. Separate them
        new = str.split(full_data.iloc[counter][0])

        # Create the time at that point
        time_str = str(new[0]) + '-' + str(new[1]) + '-' + str(new[2]) + 'T' + str(new[3][:2]) + ':00:00'

        # Make a datetime object out of time_str
        insert_time_beg = dt.datetime.strptime(time_str, '%Y-%m-%dT%H:%M:%S')

        # We know for Kp that the middle of the bin is 1.5 hours past the beginning of the bin (which is insert_time_beg)
        insert_time_mid = insert_time_beg + dt.timedelta(hours=1.5)

        # If the data point is within the time range that is desired, insert the time and the associated kp index
        # The other datasets use the dates as datetime64 objects. So must use those instead of regular datetime objects
        if insert_time_mid + dt.timedelta(hours=1.5) > ti and insert_time_mid - dt.timedelta(hours=1.5) < te:
            insert_kp = new[7]
            time = np.append(time, [insert_time_mid])
            wanted_value = np.append(wanted_value, [insert_kp])

    return time, wanted_value


def slice_dst_data(full_data, ti, te):
    start_of_first_month = dt.datetime(year=ti.year, month=ti.month, day=1)
    if te.month != 12:
        end_of_last_month = dt.datetime(year=te.year, month=te.month+1, day=1) - dt.timedelta(hours=1) + dt.timedelta(microseconds=1)
    else:
        end_of_last_month = dt.datetime(year=te.year+1, month=1, day=1) - dt.timedelta(hours=1) + dt.timedelta(microseconds=1)
    time_list = datetime_range(start_of_first_month, end_of_last_month, dt.timedelta(hours=1))

    for counter in range(0, len(full_data)):
        one_day = str.split(full_data.iloc[counter][0])
        one_day.pop(0)

        # There are some days that have large dips in dst, eclipsing -100 nT. when this happens, the whitespace between numbers is gone, and looks something like -92-105-111-119-124-109.
        # this if statement removes those strings, splits them up, and inserts them back into the one_day list as separated strings
        if len(one_day) != 24:
            for counter in range(0, 24):
                number=one_day[counter]
                if len(number) > 4:
                    too_big_number = str.split(number, '-')
                    if too_big_number[0] == '':
                        too_big_number.pop(0)
                    too_big_number = (np.array(too_big_number).astype('int64')*-1).astype('str').tolist()
                    one_day[counter:counter] = too_big_number
                    one_day.pop(counter+len(too_big_number))


        if counter==0:
            dst_list = np.array(one_day)
        else:
            dst_list = np.append(dst_list, [one_day])

    counter2=0
    while time_list[counter2] < ti:
        counter2+=1

    counter3 = len(time_list) - 1
    while time_list[counter3] > te:
        counter3-=1

    time_list = time_list[counter2:counter3]
    dst_list = dst_list[counter2:counter3]

    return time_list, dst_list


def datetime_range(start, end, delta):
    datetimes = []
    current = start
    while current < end:
        datetimes.append(current)
        current += delta
    return datetimes


def expand_5min_kp(time, kp):
    # This is for a very specific case, where you are given times at the very beginning of a day, and you only want 5 minute intervals. All other cases should be run through expand_kp

    # Expanding the kp values is easy, as np.repeat does this for us.
    # Eg: np.repeat([1,2,3],3) = [1,1,1,2,2,2,3,3,3]
    new_kp = np.repeat(kp, 36)

    new_times = np.array([])

    # Iterate through every time that is given
    for a_time in time:
        # Put the first time value into the xarray. This corresponds to the start of the kp window
        new_times = np.append(new_times, [a_time - dt.timedelta(hours=1.5)])

        # There are 36 5-minute intervals in the 3 hour window Kp covers. We have the time in the middle of the 3 hour window.
        # Here all the values are created (other than the start of the window, done above) by incrementally getting 5 minutes apart on both sides of the middle value. This is done 18 times
        # However we must make an exception when the counter is 0, because otherwise it will put the middle of the window twice
        for counter in range(18):
            if counter == 0:
                new_time = a_time
                new_times = np.append(new_times, [new_time])
            else:
                new_time_plus = a_time + counter * dt.timedelta(minutes=5)
                new_time_minus = a_time - counter * dt.timedelta(minutes=5)
                new_times = np.append(new_times, [new_time_plus, new_time_minus])

    # The datetime objects we want are created, but out of order. Put them in order
    new_times = np.sort(new_times, axis=0)

    return new_times, new_kp

def expand_kp(kp_times, kp, time_to_expand_to):
    # Note that this function is capable of doing the same thing as expand_5min, and more.

    # Also note that this function can be used for other indices and such as long as they are inputted in the same format as the Kp data is

    # Because Datetime objects that are placed into xarrays get transformed into datetime64 objects, and the conventional methods of changing them back do not seem to work,
    # You have to make a datetime64 version of the kp_times so that they can be subtracted correctly

    # Iterate through all times and convert them to datetime64 objects
    for time in kp_times:
        # The timedelta is done because the min function used later chooses the lower value in the case of a tie. We want the upper value to be chosen
        if type(time)==type(dt.datetime(2015,9,10)):
            time64 = np.datetime64(time-dt.timedelta(microseconds=1))
        elif type(time)==type(np.datetime64(1,'Y')):
            time64 = time-np.timedelta64(1,'ms') # This thing only works for 1 millisecond, not 1 microsecond. Very sad
        else:
            raise TypeError('Time array must contain either datetime or datetime64 objects')

        if time == kp_times[0]:
            datetime64_kp_times = np.array([time64])
        else:
            datetime64_kp_times = np.append(datetime64_kp_times, [time64])

    # This will be used to find the date closest to each time given
    # It will find the value closest to each given value. In other words it is used to find the closest time to each time from the given list
    absolute_difference_function = lambda list_value: abs(list_value - given_value)

    # Iterate through all times that we want to expand kp to
    for given_value in time_to_expand_to:

        # Find the closest value
        closest_value = min(datetime64_kp_times, key=absolute_difference_function)

        # Find the corresponding index of the closest value
        index = np.where(datetime64_kp_times == closest_value)

        if given_value == time_to_expand_to[0]:
            # If this is the first iteration, create an ndarray containing the kp value at the corresponding index
            new_kp = np.array([kp[index]])
        else:
            # If this is not the first iteration, combine the new kp value with the existing ones
            new_kp = np.append(new_kp, kp[index])

    return new_kp

def interpolate_data_like(data, data_like):
    data = data.interp_like(data_like)

    return data


def create_timestamps(data, ti, te):
    '''
    Convert times to UNIX timestamps.

    Parameters
    ----------
    data : `xarray.DataArray`
        Data with coordinate 'time' containing the time stamps (np.datetime64)
    ti, te : `datetime.datetime`
        Time interval of the data

    Returns
    -------
    ti, te : float
        Time interval converted to UNIX timestamps
    timestamps :
        Time stamps converted to UNIX times
    '''

    # Define the epoch and one second in np.datetime 64. This is so we can convert np.datetime64 objects to timestamp values
    unix_epoch = np.datetime64(0, 's')
    one_second = np.timedelta64(1, 's')

    # Round up the end time by one microsecond so the bins aren't marginally incorrect.
    # this is good enough for now, but this is a line to keep an eye on, as it will cause some strange things to happen due to the daylight savings fix later on.
    # if off by 1 microsecond, will erroneously gain/lose 1 hour
    if (te.second !=0) and te.second!=30:
        te = te + dt.timedelta(microseconds=1)

    ti_datetime = ti
    te_datetime = te

    # Convert the start and end times to a unix timestamp
    # This section adapts for the 4 hour time difference (in seconds) that timestamp() automatically applies to datetime. Otherwise the times from data and these time will not line up right
    # This appears because timestamp() corrects for local time difference, while the np.datetime64 method did not
    # This could be reversed and added to all the times in data, but I chose this way.
    # Note that this can cause shifts in hours if the timezone changes
    ti = ti.timestamp() - 14400
    te = te.timestamp() - 14400

    # This is to account for daylight savings
    # lazy fix: check to see if ti-te is the right amount of time. If yes, move on. If no, fix by changing te to what it should be
    # This forces the input time to be specifically 1 day of data, otherwise this number doesn't work.
    # Though maybe the 86400 could be generalized using te-ti before converting to timestamp. Possible change there
    # Though UTC is definitely something to be done, time permitting (i guess it is in UTC. need to figure out at some point)
    # This will only work for exactly 1 day of data being downloaded. It will be fine for sample and store_edi data,
    # however if running a big download that goes through a daylight savings day, there will be an issue

    if ti_datetime+dt.timedelta(days=1)==te_datetime:
        if te-ti > 86400:
            te-=3600
        elif te-ti < 86400:
            te+=3600

    # Create the array where the unix timestamp values go
    # The timestamp values are needed so we can bin the values with binned_statistic
    # Note that the data argument must be an xarray object with a 'time' dimension so that this works. Could be generalized at some point
    timestamps = (data['time'].values - unix_epoch) / one_second

    return ti, te, timestamps

def get_5min_times(data, vars_to_bin, timestamps, ti, te):
    # Get the times here. This way we don't have to rerun getting the times for every single variable that is being binned
    number_of_bins=(te-ti)/300
    count, bin_edges, binnum = binned_statistic(x=timestamps, values=data[vars_to_bin[0]], statistic='count', bins=number_of_bins, range=(ti, te))

    # Create an nparray where the new 5 minute interval datetime64 objects will go
    new_times = np.array([], dtype=object)

    # Create the datetime objects and add them to new_times
    for time in bin_edges:
        # Don't run if the time value is the last index in bin_edges. There is 1 more bin edge than there is mean values
        # This is because bin_edges includes an extra edge to encompass all the means
        # As a result, the last bin edge (when shifted to be in the middle of the dataset) doesn't correspond to a mean value
        # So it must be ignored so that the data will fit into a new dataset
        if time != bin_edges[-1]:
            # Convert timestamp to datetime object
            new_time = dt.datetime.utcfromtimestamp(time)

            # Add 2.5 minutes to place the time in the middle of each bin, rather than the beginning
            new_time = new_time + dt.timedelta(minutes=2.5)

            # Add the object to the nparray
            new_times = np.append(new_times, [new_time])

    # Return the datetime objects of the 5 minute intervals created in binned_statistic
    return new_times


def bin_5min(data, vars_to_bin, index_names, ti, te):
    '''
    Bin one day's worth of data into 5-minute intervals.

    Parameters
    ----------
    data

    vars_to_bin

    index_names

    ti, te

    Returns
    -------
    complete_data
    '''
    # Any variables that are not in var_to_bin are lost (As they can't be mapped to the new times otherwise)
    # Note that it is possible that NaN values appear in the final xarray object. This is because there were no data points in those bins
    # To remove these values, use xarray_object = test.where(np.isnan(test['variable_name']) == False, drop=True) (Variable has no indices)
    # Or xarray_object = xarray_object.where(np.isnan(test['variable_name'][:,0]) == False, drop=True) (With indices)

    # Also note that in order for this to work correctly, te-ti must be a multiple of 5 minutes.
    # This is addressed in the get_xxx_data functions, since they just extend the downloaded times by an extra couple minutes or whatever

    # In order to bin the values properly, we need to convert the datetime objects to integers. I chose to use unix timestamps to do so
    ti, te, timestamps = create_timestamps(data, ti, te)
    new_times = get_5min_times(data, vars_to_bin, timestamps, ti, te)

    number_of_bins = (te-ti)/300

    # Iterate through every variable (and associated index) in the given list
    for var_counter in range(len(vars_to_bin)):
        if index_names[var_counter] == '':
            # Since there is no index associated with this variable, there is only 1 thing to be meaned. So take the mean of the desired variable
            means, bin_edges_again, binnum = binned_statistic(x=timestamps, values=data[vars_to_bin[var_counter]], statistic='mean', bins=number_of_bins, range=(ti, te))
            std, bin_edges_again, binnum = binned_statistic(x=timestamps, values=data[vars_to_bin[var_counter]], statistic='std', bins=number_of_bins, range=(ti, te))

            # Create the dataset for the meaned variable
            new_data = xr.Dataset(coords={'time': new_times})

            # Fix the array so it will fit into the dataset
            var_values = means.T
            var_values_std = std.T

            # Put the data into the dataset
            new_data[vars_to_bin[var_counter]] = xr.DataArray(var_values, dims=['time'], coords={'time': new_times})
            new_data[vars_to_bin[var_counter]+'_std'] = xr.DataArray(var_values_std, dims=['time'], coords={'time': new_times})
        else:
            # Empty array where the mean of the desired variable will go
            means = np.array([[]])
            stds = np.array([[]])

            # Iterate through every variable in the associated index
            for counter in range(len(data[index_names[var_counter] + '_index'])):
                # Find the mean of var_to_bin
                # mean is the mean in each bin, bin_edges is the edges of each bin in timestamp values, and binnum is which values go in which bin
                mean, bin_edges_again, binnum = binned_statistic(x=timestamps, values=data[vars_to_bin[var_counter]][:, counter], statistic='mean', bins=number_of_bins, range=(ti, te))
                std, bin_edges_again, binnum = binned_statistic(x=timestamps, values=data[vars_to_bin[var_counter]][:, counter], statistic='std', bins=number_of_bins, range=(ti, te))

                # If there are no means yet, designate the solved mean value as the array where all of the means will be stored. Otherwise combine with existing data
                if means[0].size == 0:
                    means = [mean]
                    stds = [std]
                else:
                    means = np.append(means, [mean], axis=0)
                    stds = np.append(stds, [std], axis=0)

            # Create the new dataset where the 5 minute bins will go
            new_data = xr.Dataset(coords={'time': new_times, index_names[var_counter] + '_index': data[index_names[var_counter] + '_index']})

            # Format the mean values together so that they will fit into new_data
            var_values = means.T
            var_values_std = stds.T

            # Put in var_values
            new_data[vars_to_bin[var_counter]] = xr.DataArray(var_values, dims=['time', index_names[var_counter] + '_index'], coords={'time': new_times})
            new_data[vars_to_bin[var_counter]+'_std'] = xr.DataArray(var_values_std, dims=['time', index_names[var_counter] + '_index'], coords={'time': new_times})

        # If this is the first run, designate the created data as the dataset that will hold all the data. Otherwise combine with the existing data
        if var_counter == 0:
            complete_data = new_data
        else:
            complete_data = xr.merge([complete_data, new_data])

    return complete_data


def get_A(min_Lvalue, max_Lvalue):
    # E=AΦ
    # A=-⛛ (-1*Gradient)
    # In polar coordinates, A=(1/△r, 1/r△Θ), where r is the radius and Θ is the azimuthal angle
    # In this case, r=L, △r=△Θ=1

    # First we need to make the gradient operator. Since we have electric field data in bins labeled as 4.5, 5.5, ... and want potential values at integer values 1,2,...
    # We use the central difference operator to get potential values at integer values.

    # For example, E_r(L=6.5, MLT=12.5)=Φ(7,13)+Φ(7,12) - Φ(6,13)+Φ(6,12)
    # Recall matrix multiplication rules to know how we can reverse engineer each row in A knowing the above
    # So for E_r, 1 should be where Φ(7,13)+Φ(7,12) is, and -1 should be where Φ(6,13)+Φ(6,12) is
    # For the example above, that row in A looks like [0.....-1,-1, 0....1, 1, 0...0].

    # For E_az, things are slightly different, E_az(L=6.5, MLT=12.5) = 1/L * 24/2π * [Φ(7,13)+Φ(6,13)]/2 - [Φ(7,12)+Φ(6,12)]/2
    # 1/L represents the 1/r△Θ in the gradient operator, and 24/2π is the conversion from radians to MLT
    # All of the rows follow the E_r and E_az examples, and as a result A has 4 values in each row

    # This runs assuming the E vector is organized like the following:
    # E=[E_r(L=0,MLT=0), E_az(0,0), E_r(0,1), E_az(0,1)...E_r(1,0), E_az(1,0)....]
    # This may be changed later, especially if a 3rd dimension is added

    # The edge case where MLT=23.5 must be treated separately, because it has to use MLT=23 and MLT=0 as its boundaries

    L_range = int(max_Lvalue - min_Lvalue + 1)
    A = np.zeros((2 * 24 * L_range, 24 * (L_range + 1)))

    # In order to index it nicely, we must subtract the minimum value from the max value, so we can start indexing at 0
    # As a result, L_counter does not always represent the actual L value
    # In this case, the real L value is calculated by adding L_counter by min_Lvalue
    matrix_value_r = 1
    for L_counter in range(L_range):
        # This only accounts for MLT values from 0.5 to 22.5. The value where MLT = 23.5 is an exception handled at the end
        for MLT_counter in range(0, 23):
            # Here is where we implement the A values that give E_r
            A[get_A_row(L_counter, MLT_counter), get_A_col(L_counter, MLT_counter)] = -matrix_value_r
            A[get_A_row(L_counter, MLT_counter), get_A_col(L_counter, MLT_counter, 1)] = -matrix_value_r
            A[get_A_row(L_counter, MLT_counter), get_A_col(L_counter, MLT_counter, 24)] = matrix_value_r
            A[get_A_row(L_counter, MLT_counter), get_A_col(L_counter, MLT_counter, 25)] = matrix_value_r

            # Here is where we implement the A values that give E_az at the same point that the above E_r was found
            matrix_value_az = 1 * 24 / (2 * np.pi) / (L_counter + min_Lvalue)
            A[get_A_row(L_counter, MLT_counter, 1), get_A_col(L_counter, MLT_counter)] = -matrix_value_az
            A[get_A_row(L_counter, MLT_counter, 1), get_A_col(L_counter, MLT_counter, 24)] = -matrix_value_az
            A[get_A_row(L_counter, MLT_counter, 1), get_A_col(L_counter, MLT_counter, 1)] = matrix_value_az
            A[get_A_row(L_counter, MLT_counter, 1), get_A_col(L_counter, MLT_counter, 25)] = matrix_value_az

        # Where MLT=23.5 is implemented
        # E_r
        A[get_A_row(L_counter, other=46), get_A_col(L_counter, other=23)] = -matrix_value_r
        A[get_A_row(L_counter, other=46), get_A_col(L_counter)] = -matrix_value_r
        A[get_A_row(L_counter, other=46), get_A_col(L_counter, other=47)] = matrix_value_r
        A[get_A_row(L_counter, other=46), get_A_col(L_counter, other=24)] = matrix_value_r

        # E_az
        matrix_value_az = 1 * 24 / (2 * np.pi) / (L_counter + min_Lvalue)
        A[get_A_row(L_counter, other=47), get_A_col(L_counter, other=23)] = -matrix_value_az
        A[get_A_row(L_counter, other=47), get_A_col(L_counter, other=47)] = -matrix_value_az
        A[get_A_row(L_counter, other=47), get_A_col(L_counter)] = matrix_value_az
        A[get_A_row(L_counter, other=47), get_A_col(L_counter, other=24)] = matrix_value_az

    # Conversion factor between kV/Re and mV/m
    # The -1 comes from E=-⛛V. A=-⛛, therefore we need the -1 in front
    constant = -1 / 6.3712
    A *= constant

    return A


def get_A_row(L, MLT=0, other=0):
    return 48 * L + 2 * MLT + other


def get_A_col(L, MLT=0, other=0):
    return 24 * L + MLT + other


def get_C(min_Lvalue, max_Lvalue):
    # C is the hessian, or the second derivative matrix. It is used to smooth the E=AΦ relation when solving the inverse problem
    # The overall procedure used to find A is used again here (reverse engineering the values of A), however there are more values per row
    # Also, the central difference operator is not used here, so there are no halving of values
    # For the example y=Cx: y(L=6, MLT=12) = x(L=5, 12 MLT)+x(L=7, 12 MLT)+x(L=6, 11 MLT)+x(L=6, 13 MLT)-4*x(L=6, 12 MLT)

    # Like A, the edge cases must be accounted for. While the MLT edge cases can be handled the same way as in A, there are now edge cases in L.
    # The L edge cases are handled by **ignoring the lower values apparently**
    # For example, if L=4 was the lowest L value measured, then y=x(L=5, 0 MLT)+x(L=4, 23 MLT)+x(L=4, 1 MLT)-4*x(L=4, 0 MLT)

    # But, because C is a square matrix, we can use a different, much easier method to create this matrix than we did with A
    # From the above example, we know that every value down the diagonal is -4. So we can use np.diag(np.ones(dimension)) to make a square matrix with ones across the diagonal and 0 elsewhere
    # Multiplying that by -4 gives us all the -4 values we want.
    # We can use the same method to create a line of ones one above the diagonal by using np.diag(np.ones(dimension-1), 1)
    # The above method can be refactored to create a line of ones across any diagonal of the matrix
    # So we just create a couple lines of ones and add them all together to create the C matrix

    L_range = int(max_Lvalue - min_Lvalue + 1)
    MLT = 24

    # For the example y(L=6, MLT=12), this creates the -4*x(L=6, MLT=12)
    minusfour = -4 * np.diag(np.ones(MLT * (L_range + 1)))

    # These create the x(L=6, MLT=13) and x(L=6, MLT=11) respectively
    MLT_ones = np.diag(np.ones(MLT * (L_range + 1) - 1), 1)
    moreMLT_ones = np.diag(np.ones(MLT * (L_range + 1) - 1), -1)

    # These create the x(L=7, MLT=12) and x(L=5, MLT=12) respectively
    L_ones = np.diag(np.ones(MLT * (L_range + 1) - MLT), MLT)
    moreL_ones = np.diag(np.ones(MLT * (L_range + 1) - MLT), -MLT)

    # Add the ones matrices and create C
    C = minusfour + MLT_ones + moreMLT_ones + L_ones + moreL_ones

    # Nicely, this method handles the edge L cases for us, so we don't have to worry about those.
    # However we do need to handle the edge MLT cases, since both MLT=0 and MLT=23 are incorrect as is

    # This loop fixes all the edge cases except for the very first and very last row in C, as they are fixed differently than the rest
    for counter in range(1, L_range+1):
        # Fixes MLT=23
        C[MLT * counter - 1][MLT*counter] = 0
        C[MLT * counter - 1][MLT*(counter-1)] = 1

        # Fixes MLT=0 at the L value 1 higher than the above statement
        C[MLT * counter][MLT * counter - 1] = 0
        C[MLT * counter][MLT * (counter+1) - 1] = 1

    # Fixes the first row
    C[0][MLT-1] = 1
    # Fixes the last row
    C[MLT*(L_range+1)-1][MLT*L_range] = 1

    return C

def calculate_potential(imef_data, name_of_variable):
    # Determine the L range that the data uses
    min_Lvalue = imef_data['L'][0, 0].values
    max_Lvalue = imef_data['L'][-1, 0].values

    # Find the number of bins relative to L and MLT
    # nL is the number of L values in E, not Φ. So there will be nL+1 in places. There are 6 L values in E, but 7 in Φ (As L is taken at values of 4.5, 5.5, etc in E, but 4, 5, etc in Φ)
    nL = int(max_Lvalue - min_Lvalue + 1)
    nMLT = 24

    # Get the electric field data and make them into vectors. MUST BE POLAR COORDINATES
    E_r = imef_data[name_of_variable][:, :, 0].values.flatten()
    E_az = imef_data[name_of_variable][:, :, 1].values.flatten()

    # Create the number of elements that the potential will have
    nElements = 24 * nL
    E = np.zeros(2 * nElements)

    # Reformat E_r and E_az so that they are combined into 1 vector following the format
    # [E_r(L=4, MLT=0), E_az(L=4, MLT=0), E_r(L=4, MLT=1), E_az(L=4, MLT=1), ... E_r(L=5, MLT=0), E_az(L=5, MLT=0), ...]
    for index in range(0, nElements):
        E[2 * index] = E_r[index]
        E[2 * index + 1] = E_az[index]

    # Create the A matrix
    A = get_A(min_Lvalue, max_Lvalue)

    # Create the C matrix
    C = get_C(min_Lvalue, max_Lvalue)

    # Define the tradeoff parameter γ
    gamma = 2.51e-4

    # Solve the inverse problem according to the equation in Matsui 2004 and Korth 2002
    # V=(A^T * A + γ * C^T * C)^-1 * A^T * E
    V = np.dot(np.dot(np.linalg.inv(np.dot(A.T, A) + gamma * np.dot(C.T, C)), A.T), E)
    V = V.reshape(nL + 1, nMLT)

    return V

def calculate_potential_2(imef_data, name_of_variable, guess):
    # Determine the L range that the data uses
    min_Lvalue = imef_data['L'][0, 0].values
    max_Lvalue = imef_data['L'][-1, 0].values

    # Find the number of bins relative to L and MLT
    # nL is the number of L values in E, not Φ. So there will be nL+1 in places. There are 6 L values in E, but 7 in Φ (As L is taken at values of 4.5, 5.5, etc in E, but 4, 5, etc in Φ)
    nL = int(max_Lvalue - min_Lvalue + 1)
    nMLT = 24

    # Get the electric field data and make them into vectors. MUST BE POLAR COORDINATES
    E_r = imef_data[name_of_variable][:, :, 0].values.flatten()
    E_az = imef_data[name_of_variable][:, :, 1].values.flatten()

    # Create the number of elements that the potential will have
    nElements = 24 * int(max_Lvalue - min_Lvalue + 1)
    E = np.zeros(2 * nElements)

    # Reformat E_r and E_az so that they are combined into 1 vector following the format
    # [E_r(L=4, MLT=0), E_az(L=4, MLT=0), E_r(L=4, MLT=1), E_az(L=4, MLT=1), ... E_r(L=5, MLT=0), E_az(L=5, MLT=0), ...]
    for index in range(0, nElements):
        E[2 * index] = E_r[index]
        E[2 * index + 1] = E_az[index]

    # Create the A matrix
    A = get_A(min_Lvalue, max_Lvalue)

    # Create the C matrix
    C = get_C(min_Lvalue, max_Lvalue)

    # Define the tradeoff parameter γ
    gamma = 2.51e-4

    # HERE IS THE DIFFERENCES FROM CALCULATE_POTENTIAL

    def loss(v, A, E, C, gamma):
        function = np.dot(np.transpose(np.dot(A,v)-E),(np.dot(A,v)-E))+ gamma * np.dot(np.dot(np.transpose(v),C),v)
        return function

    def grad_loss(v, A, E, C, gamma):
        return 2 * np.transpose(A) @ A @ v - 2*np.transpose(A)@E + 2*gamma*np.transpose(C)@C@v

    # Solve the inverse problem according to the equation in Matsui 2004 and Korth 2002
    V = optimize.minimize(loss, guess, args=(A, E, C, gamma), method="CG", jac=grad_loss)
    optimized = V.x
    # V = V.reshape(nL + 1, nMLT)

    return V