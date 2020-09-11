# -*- coding: utf-8 -*-
"""
Created on: Wed Jun 17 11:50:10 2020
Updated on: Wed Sep 09 14:07:24 2020

Original author: tyu2
Last Update Made by: Ben Taylor

File purpose:
NHB production and distribution + PA 2 to OD conversion
returned from Systra phase 2 contract
"""

import os

import numpy as np
import pandas as pd
import itertools

from typing import List

import pa_to_od as pa2od
import efs_constants as consts
import external_forecast_system as base_efs
from demand_utilities import tms_utils as dut
from demand_utilities import efs_utils as due

# Global paths
home_path = 'Y:/NorMITs Demand/'
lookup_path = os.path.join(home_path, 'import')
import_path = os.path.join(home_path, 'inputs/default/tp_pa')
export_path = "C:/Users/Sneezy/Desktop/NorMITs Demand/nhb_dev"
seed_distributions_path = os.path.join(home_path,
                                       'inputs',
                                       'distributions',
                                       'tms',
                                       'PA Matrices 24hr')

MODEL_NAME = 'norms'

_default_lookup_folder = 'Y:/NorMITs Demand/import/phi_factors'


def _build_tp_pa_internal(pa_import,
                          pa_export,
                          trip_origin,
                          matrix_format,
                          year,
                          purpose,
                          mode,
                          segment,
                          car_availability,
                          model_zone,
                          tp_split):
    """
    The internals of build_tp_pa(). Useful for making the code more
    readable due to the number of nested loops needed

    Returns
    -------

    """
    # ## Read in 24hr matrix ## #
    productions_fname = due.get_dist_name(
        trip_origin,
        matrix_format,
        str(year),
        str(purpose),
        str(mode),
        str(segment),
        str(car_availability),
        csv=True
    )
    pa_24hr = pd.read_csv(os.path.join(pa_import, productions_fname))

    # Convert from wide to long format
    y_zone = 'a_zone' if model_zone == 'p_zone' else 'd_zone'
    pa_24hr = due.expand_distribution(
        pa_24hr,
        year,
        purpose,
        mode,
        segment,
        car_availability,
        id_vars=model_zone,
        var_name=y_zone,
        value_name='trips'
    )

    # ## Narrow tp_split down to just the segment here ## #
    segment_id = 'soc_id' if purpose in [1, 2] else 'ns_id'
    segmentation_mask = due.get_segmentation_mask(
        tp_split,
        col_vals={
            'purpose_id': purpose,
            'mode_id': mode,
            segment_id: str(segment),
            'car_availability_id': car_availability,
        },
        ignore_missing_cols=True
    )
    tp_split = tp_split.loc[segmentation_mask]
    tp_split = tp_split.reindex([model_zone, 'tp', 'trips'], axis=1)

    # ## Calculate the time split factors for each zone ## #
    unq_zone = tp_split[model_zone].drop_duplicates()
    for zone in unq_zone:
        zone_mask = (tp_split[model_zone] == zone)
        tp_split.loc[zone_mask, 'time_split'] = (
                tp_split[zone_mask]['trips'].values
                /
                tp_split[zone_mask]['trips'].sum()
        )
    time_splits = tp_split.reindex(
        [model_zone, 'tp', 'time_split'],
        axis=1
    )

    # ## Apply tp-split factors to total pa_24hr ## #
    unq_time = time_splits['tp'].drop_duplicates()
    for time in unq_time:
        # Need to do a left join, and set any missing vals. Ensures
        # zones don't go missing if there's an issue with tp_split input
        # NOTE: tp3 is missing for p2, m1, soc0, ca1
        time_factors = time_splits.loc[time_splits['tp'] == time]
        gb_tp = pd.merge(
            pa_24hr,
            time_factors,
            on=[model_zone],
            how='left'
        ).rename(columns={'trips': 'dt'})
        gb_tp['time_split'] = gb_tp['time_split'].fillna(0)
        gb_tp['tp'] = gb_tp['tp'].fillna(time).astype(int)

        # Calculate the number of trips for this time_period
        gb_tp['dt'] = gb_tp['dt'] * gb_tp['time_split']

        # ## Aggregate back up to our segmentation ## #
        all_seg_cols = [
            model_zone,
            y_zone,
            "purpose_id",
            "mode_id",
            "soc_id",
            "ns_id",
            "car_availability_id",
            "tp"
        ]

        # Get rid of cols we're not using
        seg_cols = [x for x in all_seg_cols if x in gb_tp.columns]
        gb_tp = gb_tp.groupby(seg_cols)["dt"].sum().reset_index()

        # Build write path
        tp_pa_name = due.get_dist_name(
            str(trip_origin),
            'pa',
            str(year),
            str(purpose),
            str(mode),
            str(segment),
            str(car_availability),
            tp=str(time)
        )
        tp_pa_fname = tp_pa_name + '.csv'
        out_tp_pa_path = os.path.join(
            pa_export,
            tp_pa_fname
        )

        # Convert table from long to wide format and save
        gb_tp.rename(
            columns={model_zone: 'norms_zone_id'}
        ).pivot_table(
            index='norms_zone_id',
            columns=y_zone,
            values='dt'
        ).to_csv(out_tp_pa_path)


def build_tp_pa(tp_import: str,
                pa_import: str,
                pa_export: str,
                year_string_list: str,
                required_purposes: List[int],
                required_modes: List[int],
                required_soc: List[int] = None,
                required_ns: List[int] = None,
                required_ca: List[int] = None,
                matrix_format: str = 'pa'
                ) -> None:
    """
    Converts the 24hr matrices in pa_import into time_period segmented
    matrices - outputting to pa_export

    Parameters
    ----------
    tp_import:
        Path to the dir containing the seed values to use for splitting
        pa_import matrices by tp

    pa_import:
        Path to the dir containing the 24hr matrices

    pa_export:
        Path to the dir to export the tp split matrices

    year_string_list:
        A list of which years of 24hr Matrices to convert.

    required_purposes:
        A list of which purposes of 24hr Matrices to convert.

    required_modes:
        A list of which modes of 24hr Matrices to convert.

    required_soc:
        A list of which soc of 24hr Matrices to convert.

    required_ns:
        A list of which ns of 24hr Matrices to convert.

    required_ca:
        A list of which car availabilities of 24hr Matrices to convert.

    matrix_format:
        Which format the matrix is in. Either 'pa' or 'od'

    Returns
    -------
        None

    """
    # Arg init
    if matrix_format not in consts.VALID_MATRIX_FORMATS:
        raise ValueError("'%s' is not a valid matrix format."
                         % str(matrix_format))

    # TODO: Infer these arguments based on pa_import
    #  Along with yr, p, m
    required_soc = [None] if required_soc is None else required_soc
    required_ns = [None] if required_ns is None else required_ns
    required_ca = [None] if required_ca is None else required_ca

    # Loop Init
    if matrix_format == 'pa':
        model_zone = 'p_zone'
    elif matrix_format == 'od':
        model_zone = 'o_zone'
    else:
        # Shouldn't be able to get here
        raise ValueError("'%s' seems to be a valid matrix format, "
                         "but build_tp_pa() cannot handle it. Sorry :(")

    # For every: Year, purpose, mode, segment, ca
    for year in year_string_list:
        print("\nYear: %s" % str(year))
        for purpose in required_purposes:
            print("\tPurpose: %s" % str(purpose))

            # Purpose specific set-up
            # Do it here to avoid repeats in inner loops
            if purpose in consts.ALL_NHB_P:
                # TODO: How to allocate tp to NHB
                print('\tNHB run')
                trip_origin = 'nhb'
                required_segments = [None]
                tp_split_fname = 'export_nhb_productions_norms.csv'
                tp_split_path = os.path.join(tp_import, tp_split_fname)

            elif purpose in consts.ALL_HB_P:
                print('\tHB run')
                trip_origin = 'hb'
                tp_split_fname = 'export_productions_norms.csv'
                tp_split_path = os.path.join(tp_import, tp_split_fname)
                if purpose in [1, 2]:
                    required_segments = required_soc
                else:
                    required_segments = required_ns

            else:
                raise ValueError("%s is not a valid purpose."
                                 % str(purpose))

            # TODO: @Chris: is this the correct time split file to use?
            #  Should use TMS base year tp PA as seed?
            #  For example - TMS pa_to_od uses:
            #  Y:\NorMITs Synthesiser\Noham\iter8a\Production Outputs/hb_productions_noham.csv

            # Read in the seed values for tp splits
            tp_split = pd.read_csv(tp_split_path).rename(
                columns={
                    'norms_zone_id': model_zone,
                    'p': 'purpose_id',
                    'm': 'mode_id',
                    'soc': 'soc_id',
                    'ns': 'ns_id',
                    'ca': 'car_availability_id',
                    'time': 'tp'
                }
            )
            tp_split[model_zone] = tp_split[model_zone].astype(int)

            # Compile aggregate to p/m if NHB
            if trip_origin == 'nhb':
                tp_split = tp_split.groupby(
                    [model_zone, 'purpose_id', 'mode_id', 'tp']
                )['trips'].sum().reset_index()

            for mode in required_modes:
                print("\t\tMode: %s" % str(mode))
                for segment in required_segments:
                    for car_availability in required_ca:
                        _build_tp_pa_internal(
                            pa_import,
                            pa_export,
                            trip_origin,
                            matrix_format,
                            year,
                            purpose,
                            mode,
                            segment,
                            car_availability,
                            model_zone,
                            tp_split
                        )
    return


def _build_od_internal(pa_import,
                       od_export,
                       model_name,
                       calib_params,
                       lookup_folder,
                       phi_type,
                       aggregate_to_wday,
                       echo=True):
    """
    The internals of build_od(). Useful for making the code more
    readable due to the number of nested loops needed

    TODO: merge with TMS - NOTE:
    All this code below has been mostly copied from TMS pa_to_od.py
    function of the same name. A few filenames etc have been changed
    to make sure it properly works with NorMITs demand files (This is
    due to NorMITs demand needing moving in entirety over to the Y drive)

    Returns
    -------

    """
    # Init
    tps = ['tp1', 'tp2', 'tp3', 'tp4']
    matrix_totals = list()
    dir_contents = os.listdir(pa_import)
    mode = calib_params['m']
    purpose = calib_params['p']
    # model_zone_col = 'p_zone'
    model_zone_col = model_name.lower() + '_zone_id'

    # Get appropriate phis and filter
    phi_factors = pa2od.get_time_period_splits(
        mode,
        phi_type,
        aggregate_to_wday=aggregate_to_wday,
        lookup_folder=lookup_folder,
        echo=echo)
    phi_factors = pa2od.simplify_time_period_splits(phi_factors)
    phi_factors = phi_factors[phi_factors['purpose_from_home'] == purpose]

    # Get the relevant filenames from the dir
    dir_subset = dir_contents.copy()
    for name, param in calib_params.items():
        # Work around for 'p2' clashing with 'tp2'
        if name == 'p':
            dir_subset = [x for x in dir_subset if '_' + name + str(param) in x]
        else:
            dir_subset = [x for x in dir_subset if (name + str(param)) in x]

    # Build dict of tp names to filenames
    tp_names = {}
    for tp in tps:
        tp_names.update({tp: [x for x in dir_subset if tp in x][0]})

    # ## Build from_home dict from imported from_home PA ## #
    frh_dist = {}
    for tp, path in tp_names.items():
        dist_df = pd.read_csv(os.path.join(pa_import, path))
        zone_nums = dist_df[model_zone_col]     # Save to re-attach later
        dist_df = dist_df.drop(model_zone_col, axis=1)
        frh_dist.update({tp: dist_df})

    # ## Build to_home matrices from the from_home PA ## #
    frh_ph = {}
    for tp_frh in tps:
        dut.print_w_toggle('From from_h ' + str(tp_frh), echo=echo)
        frh_int = int(tp_frh.replace('tp', ''))
        phi_frh = phi_factors[phi_factors['time_from_home'] == frh_int]

        # Transpose to flip P & A
        frh_base = frh_dist[tp_frh].copy()
        # print(frh_base.shape)
        # print(calib_params)
        # print(tp_frh)
        frh_base = frh_base.values.T

        toh_dists = {}
        for tp_toh in tps:
            # Get phi
            dut.print_w_toggle('\tBuilding to_h ' + str(tp_toh), echo=echo)
            toh_int = int(tp_toh.replace('tp', ''))
            phi_toh = phi_frh[phi_frh['time_to_home'] == toh_int]
            phi_toh = phi_toh['direction_factor']

            # Cast phi toh
            phi_mat = np.broadcast_to(phi_toh,
                                      (len(frh_base),
                                       len(frh_base)))
            tp_toh_mat = frh_base * phi_mat
            toh_dists.update({tp_toh: tp_toh_mat})
        frh_ph.update({tp_frh: toh_dists})

    # ## Aggregate to_home matrices by time period ## #
    # removes the from_home splits
    tp1_list = list()
    tp2_list = list()
    tp3_list = list()
    tp4_list = list()
    for item, toh_dict in frh_ph.items():
        for toh_tp, toh_dat in toh_dict.items():
            if toh_tp == 'tp1':
                tp1_list.append(toh_dat)
            elif toh_tp == 'tp2':
                tp2_list.append(toh_dat)
            elif toh_tp == 'tp3':
                tp3_list.append(toh_dat)
            elif toh_tp == 'tp4':
                tp4_list.append(toh_dat)

    toh_dist = {
        'tp1': np.sum(tp1_list, axis=0),
        'tp2': np.sum(tp2_list, axis=0),
        'tp3': np.sum(tp3_list, axis=0),
        'tp4': np.sum(tp4_list, axis=0)
    }

    # ## Output the from_home and to_home matrices ## #
    for tp in tps:
        # Get output matrices
        output_name = tp_names[tp]

        output_from = frh_dist[tp]
        from_total = output_from.sum().sum()
        output_from_name = output_name.replace('pa', 'od_from')

        output_to = toh_dist[tp]
        to_total = output_to.sum().sum()
        output_to_name = output_name.replace('pa', 'od_to')

        # ## Gotta fudge the row/column names ## #
        # Add the zone_nums back on
        output_from = pd.DataFrame(output_from).reset_index()
        # noinspection PyUnboundLocalVariable
        output_from['index'] = zone_nums
        output_from.columns = [model_zone_col] + zone_nums.tolist()
        output_from = output_from.set_index(model_zone_col)

        output_to = pd.DataFrame(output_to).reset_index()
        output_to['index'] = zone_nums
        output_to.columns = [model_zone_col] + zone_nums.tolist()
        output_to = output_to.set_index(model_zone_col)

        # With columns fixed, created full OD output
        output_od = output_from + output_to
        output_od_name = output_name.replace('pa', 'od')

        dut.print_w_toggle('Exporting ' + output_from_name, echo=echo)
        dut.print_w_toggle('& ' + output_to_name, echo=echo)
        dut.print_w_toggle('& ' + output_od_name, echo=echo)
        dut.print_w_toggle('To ' + od_export, echo=echo)

        # Output from_home, to_home and full OD matrices
        output_from_path = os.path.join(od_export, output_from_name)
        output_to_path = os.path.join(od_export, output_to_name)
        output_od_path = os.path.join(od_export, output_od_name)
        output_from.to_csv(output_from_path)
        output_to.to_csv(output_to_path)
        output_od.to_csv(output_od_path)

        matrix_totals.append([output_name, from_total, to_total])

    dist_name = due.get_dist_name_from_calib_params('hb', 'od', calib_params)
    print("INFO: OD Matrices for %s written to file." % dist_name)
    return matrix_totals


def build_od(pa_import,
             od_export,
             required_purposes,
             required_modes,
             required_soc,
             required_ns,
             required_car_availabilities,
             year_string_list,
             lookup_folder=_default_lookup_folder,
             phi_type='fhp_tp',
             aggregate_to_wday=True,
             echo=True):
    """
    This function imports time period split factors from a given path.W
    """
    # For every: Year, purpose, mode, segment, ca
    matrix_totals = list()
    for year in year_string_list:
        for purpose in required_purposes:
            required_segments = required_soc if purpose in [1, 2] else required_ns
            for mode in required_modes:
                for segment in required_segments:
                    for ca in required_car_availabilities:
                        calib_params = due.generate_calib_params(
                            year,
                            purpose,
                            mode,
                            segment,
                            ca
                        )
                        segmented_matrix_totals = _build_od_internal(
                            pa_import,
                            od_export,
                            MODEL_NAME,
                            calib_params,
                            lookup_folder,
                            phi_type,
                            aggregate_to_wday,
                            echo=echo
                        )
                        matrix_totals += segmented_matrix_totals
    return matrix_totals


def _nhb_production_internal(hb_pa_import,
                             nhb_trip_rates,
                             year,
                             purpose,
                             mode,
                             segment,
                             car_availability):
    """
      The internals of nhb_production(). Useful for making the code more
      readable due to the number of nested loops needed
    """
    hb_dist = due.get_dist_name(
        'hb',
        'pa',
        str(year),
        str(purpose),
        str(mode),
        str(segment),
        str(car_availability),
        csv=True
    )

    # Seed the nhb productions with hb values
    hb_productions = pd.read_csv(
        os.path.join(hb_pa_import, hb_dist)
    )
    hb_productions = due.expand_distribution(
        hb_productions,
        year,
        purpose,
        mode,
        segment,
        car_availability,
        id_vars='p_zone',
        var_name='a_zone',
        value_name='trips'
    )

    # Aggregate to destinations
    nhb_productions = hb_productions.groupby([
        "a_zone",
        "purpose_id",
        "mode_id",
        "car_availability_id",
        "soc_id",
        "ns_id"
    ])["trips"].sum().reset_index()

    # join nhb trip rates
    nhb_productions = nhb_trip_rates.merge(
        nhb_productions,
        on=["purpose_id", "mode_id"]
    )

    # Calculate NHB productions
    nhb_productions["nhb_dt"] = nhb_productions["trips"] * nhb_productions[
        "nhb_trip_rate"]

    # aggregate nhb_p 11_12
    nhb_productions.loc[nhb_productions["nhb_p"] == 11, "nhb_p"] = 12

    # Remove hb purpose and mode by aggregation
    nhb_productions = nhb_productions.groupby([
        "a_zone",
        "nhb_p",
        "nhb_m",
        "car_availability_id",
        "soc_id",
        "ns_id"
    ])["nhb_dt"].sum().reset_index()

    return nhb_productions


def nhb_production(hb_pa_import,
                   nhb_export,
                   model_name,
                   required_purposes,
                   required_modes,
                   required_soc,
                   required_ns,
                   required_car_availabilities,
                   year_string_list,
                   lookup_folder,
                   nhb_productions_fname='internal_nhb_productions.csv'):
    """
    This function builds NHB productions by
    aggregates HB distribution from EFS output to destination

    TODO: Does this need updating to use the TMS method?

    Parameters
    ----------
    required lists:
        to loop over TfN segments

    Returns
    ----------
    nhb_production_dictionary:
        Dictionary containing NHB productions by year
    """
    # Init
    yearly_nhb_productions = list()
    nhb_production_dictionary = dict()
    model_zone_col = model_name.lower() + '_zone_id'

    # Get nhb trip rates
    nhb_trip_rates = pd.read_csv(
        os.path.join(lookup_folder, "IgammaNMHM.csv")
    ).rename(
        columns={"p": "purpose_id", "m": "mode_id"}
    )

    # For every: Year, purpose, mode, segment, ca
    for year in year_string_list:
        loop_gen = due.segmentation_loop_generator(required_purposes,
                                                   required_modes,
                                                   required_soc,
                                                   required_ns,
                                                   required_car_availabilities)
        for purpose, mode, segment, car_availability in loop_gen:
            nhb_productions = _nhb_production_internal(
                hb_pa_import,
                nhb_trip_rates,
                year,
                purpose,
                mode,
                segment,
                car_availability
            )
            yearly_nhb_productions.append(nhb_productions)

        # ## Output the yearly productions ## #
        # Aggregate all productions for this year
        print("INFO: NHB Productions for yr%d complete!" % year)
        yr_nhb_productions = pd.concat(yearly_nhb_productions)
        yearly_nhb_productions.clear()

        # Rename columns from NHB perspective
        yr_nhb_productions = yr_nhb_productions.rename(
            columns={
                'a_zone': 'p_zone',
                'nhb_p': 'p',
                'nhb_m': 'm',
                'nhb_dt': 'trips'
            }
        )

        # Create year fname
        nhb_productions_fname = '_'.join(
            ["yr" + str(year), nhb_productions_fname]
        )

        # Output disaggregated
        da_fname = due.add_fname_suffix(nhb_productions_fname, '_disaggregated')
        yr_nhb_productions.to_csv(
            os.path.join(nhb_export, da_fname),
            index=False
        )

        # Aggregate productions up to p/m level
        yr_nhb_productions = yr_nhb_productions.groupby(
            ["p_zone", "p", "m"]
        )["trips"].sum().reset_index()

        # Rename cols and output to file
        # Output at p/m aggregation
        yr_nhb_productions.to_csv(
            os.path.join(nhb_export, nhb_productions_fname),
            index=False
        )

        # save to dictionary by year
        nhb_production_dictionary[year] = yr_nhb_productions
   
    return nhb_production_dictionary

                        
def nhb_furness(p_import,
                seed_nhb_dist_dir,
                od_export,
                required_purposes,
                required_modes,
                year_string_list,
                replace_zero_vals,
                zero_infill,
                nhb_productions_fname='internal_nhb_productions.csv',
                use_zone_id_subset=False):

    """
    Provides a one-iteration Furness constrained on production
    with options whether to replace zero values on the seed

    Essentially distributes the Productions based on the seed nhb dist
    TODO: Actually add in some furnessing

    Return:
    ----------
    None
    """
    # TODO: Add in file exists checks

    # For every year, purpose, mode
    yr_p_m_iter = itertools.product(year_string_list,
                                    required_purposes,
                                    required_modes)
    for year, purpose, mode in yr_p_m_iter:
        # ## Read in Files ## #
        # Create year fname
        year_p_fname = '_'.join(
            ["yr" + str(year), nhb_productions_fname]
        )

        # Read in productions
        p_path = os.path.join(p_import, year_p_fname)
        productions = pd.read_csv(p_path)

        # select needed productions
        productions = productions.loc[productions["p"] == purpose]
        productions = productions.loc[productions["m"] == mode]

        # read in nhb_seeds
        seed_fname = due.get_dist_name(
            'nhb',
            'pa',
            purpose=str(purpose),
            mode=str(mode),
            csv=True
        )
        nhb_seeds = pd.read_csv(os.path.join(seed_nhb_dist_dir, seed_fname))

        # convert from wide to long format
        nhb_seeds = nhb_seeds.melt(
            id_vars=['p_zone'],
            var_name='a_zone',
            value_name='seed_vals'
        )

        # Need to make sure they are the correct types
        nhb_seeds['a_zone'] = nhb_seeds['a_zone'].astype(float).astype(int)
        productions['p_zone'] = productions['p_zone'].astype(int)

        if use_zone_id_subset:
            zone_subset = [259, 267, 268, 270, 275, 1171, 1173]
            nhb_seeds = base_efs.get_data_subset(
                nhb_seeds, 'p_zone', zone_subset)
            nhb_seeds = base_efs.get_data_subset(
                nhb_seeds, 'a_zone', zone_subset)

        # Check the productions and seed zones match
        p_zones = set(productions["p_zone"].tolist())
        seed_zones = set(nhb_seeds["p_zone"].tolist())
        if p_zones != seed_zones:
            raise ValueError("Production and seed attraction zones "
                             "do not match.")

        # Infill zero values
        if replace_zero_vals:
            mask = (nhb_seeds["seed_vals"] == 0)
            nhb_seeds.loc[mask, "seed_vals"] = zero_infill

        # Calculate seed factors by zone
        # (The sum of zone seed factors should equal 1)
        unq_zone = nhb_seeds['p_zone'].drop_duplicates()
        for zone in unq_zone:
            zone_mask = (nhb_seeds['p_zone'] == zone)
            nhb_seeds.loc[zone_mask, 'seed_factor'] = (
                    nhb_seeds[zone_mask]['seed_vals'].values
                    /
                    nhb_seeds[zone_mask]['seed_vals'].sum()
            )
        nhb_seeds = nhb_seeds.reindex(
            ['p_zone', 'a_zone', 'seed_factor'],
            axis=1
        )

        # Use the seed factors to Init P-A trips
        init_pa = pd.merge(
            nhb_seeds,
            productions,
            on=["p_zone"])
        init_pa["trips"] = init_pa["seed_factor"] * init_pa["trips"]

        # TODO: Some actual furnessing should happen here!
        final_pa = init_pa

        # ## Output the furnessed PA matrix to file ## #
        # Generate path
        nhb_dist_fname = due.get_dist_name(
            'nhb',
            'od',
            str(year),
            str(purpose),
            str(mode),
            csv=True
        )
        out_path = os.path.join(od_export, nhb_dist_fname)

        # Convert from long to wide format and output
        # TODO: Generate output name based on model name
        final_pa.rename(
            columns={'p_zone': 'norms_zone_id'}
        ).pivot_table(
            index='norms_zone_id',
            columns='a_zone',
            values='trips'
        ).to_csv(out_path)
        print("NHB Distribution %s complete!" % nhb_dist_fname)


def main():
    # TODO: Integrate into TMS and EFS proper

    # Say what to run
    run_build_tp_pa = False
    run_build_od = False
    run_nhb_production = False
    run_nhb_furness = False
    run_nhb_build_tp_pa = True

    # TODO: Properly integrate this
    # How much should we print?
    echo = False

    # TODO: Create output folders
    # dut.create_folder(pa_export, chDir=False)

    if run_build_tp_pa:
        build_tp_pa(tp_import=import_path,
                    pa_import=os.path.join(export_path, '24hr PA Matrices'),
                    pa_export=os.path.join(export_path, 'PA Matrices'),
                    year_string_list=consts.NHB_FUTURE_YEARS,
                    required_purposes=consts.PURPOSES_NEEDED,
                    required_modes=consts.MODES_NEEDED,
                    required_soc=consts.SOC_NEEDED,
                    required_ns=consts.NS_NEEDED, required_ca=consts.CA_NEEDED)
        print('Transposed HB PA to tp PA\n')

    if run_build_od:
        build_od(
            pa_import=os.path.join(export_path, "PA Matrices"),
            od_export=os.path.join(export_path, "OD Matrices"),
            required_purposes=consts.PURPOSES_NEEDED,
            required_modes=consts.MODES_NEEDED,
            required_soc=consts.SOC_NEEDED,
            required_ns=consts.NS_NEEDED,
            required_car_availabilities=consts.CA_NEEDED,
            year_string_list=consts.NHB_FUTURE_YEARS,
            phi_type='fhp_tp',
            aggregate_to_wday=True,
            echo=echo
        )
        print('Transposed HB tp PA to OD\n')

    # TODO: Create 24hr OD for HB

    if run_nhb_production:
        nhb_production(
            hb_pa_import=os.path.join(export_path, "24hr PA Matrices"),
            nhb_export=os.path.join(export_path, "Productions"),
            model_name=MODEL_NAME,
            required_purposes=consts.PURPOSES_NEEDED,
            required_modes=consts.NHB_MODES_NEEDED,
            required_soc=consts.SOC_NEEDED,
            required_ns=consts.NS_NEEDED,
            required_car_availabilities=consts.CA_NEEDED,
            year_string_list=consts.NHB_FUTURE_YEARS,
            lookup_folder=lookup_path)
        print('Generated NHB productions\n')

    if run_nhb_furness:
        nhb_furness(
            p_import=os.path.join(export_path, "Productions"),
            seed_nhb_dist_dir=seed_distributions_path,
            od_export=os.path.join(export_path, "24hr OD Matrices"),
            required_purposes=consts.NHB_PURPOSES_NEEDED,
            required_modes=consts.NHB_MODES_NEEDED,
            year_string_list=consts.NHB_FUTURE_YEARS,
            replace_zero_vals=True,
            zero_infill=0.01,
            use_zone_id_subset=True)
        print('"Furnessed" NHB Productions\n')

    if run_nhb_build_tp_pa:
        build_tp_pa(tp_import=import_path,
                    pa_import=os.path.join(export_path, '24hr OD Matrices'),
                    pa_export=os.path.join(export_path, 'OD Matrices'),
                    matrix_format='od',
                    year_string_list=consts.NHB_FUTURE_YEARS,
                    required_purposes=consts.NHB_PURPOSES_NEEDED,
                    required_modes=consts.NHB_MODES_NEEDED)
        print('Transposed NHB OD to tp OD\n')


if __name__ == '__main__':
    main()
