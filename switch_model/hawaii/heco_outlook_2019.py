from __future__ import division
from __future__ import print_function
from collections import defaultdict
from textwrap import dedent
import os
from pyomo.environ import *
import pandas as pd
import time

def TODO(note):
    raise NotImplementedError(dedent(note).strip())

def NOTE(note):
    print("=" * 80)
    print("{}:".format(__name__))
    print(dedent(note).strip())
    print("=" * 80)
    print()
    # time.sleep(2)

def define_arguments(argparser):
    argparser.add_argument('--psip-force', action='store_true', default=True,
        help="Force following of PSIP plans (building exact amounts of certain technologies).")
    argparser.add_argument('--psip-relax', dest='psip_force', action='store_false',
        help="Relax PSIP plans, to find a more optimal strategy.")
    argparser.add_argument('--psip-minimal-renewables', action='store_true', default=False,
        help="Use only the amount of renewables shown in PSIP plans, and no more (should be combined with --psip-relax).")
    argparser.add_argument('--force-build', nargs=3, default=None,
        help="Force construction of at least a certain quantity of a particular technology during certain years. Space-separated list of year, technology and quantity.")
    argparser.add_argument('--psip-relax-after', type=float, default=None,
        help="Follow the PSIP plan up to and including the specified year, then optimize construction in later years. Should be combined with --psip-force.")

def is_renewable(tech):
    return any(txt in tech for txt in ("PV", "Wind", "Solar"))
def is_battery(tech):
    return 'battery' in tech.lower()

def define_components(m):
    ###################
    # resource rules to match HECO's forecast as of late 2019 or
    # (optionally) 2016-12 PSIP
    ##################

    # decide whether to enforce the PSIP preferred plan
    # if an environment variable is set, that takes precedence
    # (e.g., on a cluster to override options.txt)
    psip_env_var = os.environ.get('USE_PSIP_PLAN')
    if psip_env_var is None:
        # no environment variable; use the --psip-relax flag
        psip = m.options.psip_force
    elif psip_env_var.lower() in ["1", "true", "y", "yes", "on"]:
        psip = True
    elif psip_env_var.lower() in ["0", "false", "n", "no", "off"]:
        psip = False
    else:
        raise ValueError('Unrecognized value for environment variable USE_PSIP_PLAN={} (should be 0 or 1)'.format(psip_env_var))

    if m.options.verbose:
        if psip:
            print("Using PSIP construction plan.")
        else:
            print("Relaxing PSIP construction plan (optimizing around forecasted adoption).")

    # make sure LNG is turned off
    if (
        psip
        and 'LNG' in m.FUELS
        and getattr(m.options, "force_lng_tier", []) != ["none"]
    ):
        raise RuntimeError(
            'To match the PSIP with LNG available, you must use the lng_conversion '
            'module and set "--force-lng-tier none".'
        )

    # use cases:
    # DistPV fixed all the way through for most-likely scenarios and PSIP scenarios but not for general Switch-Oahu
    # Distributed storage fixed all the way through in most-likely and PSIP but not Switch-Oahu
    # Centralized storage Battery_Bulk at lower limit all the way through (representing distributed storage) in
    # Large PV, Onshore Wind, Offshore Wind, centralized storage fixed for some early years in most-likely case and PSIP, maybe in Switch-Oahu
    # Other technologies at fixed levels in PSIP but not most-likely case
    # In most-likely and PSIP scenarios, all renewables already in place plus everything specified in targets gets rebuilt at retirement.

    # Plan:
    # - each year is either fixed or flexible, i.e., early years will have predetermined build or not
    # - when PSIP is in effect, all targets are exact -- no construction possible except what's listed
    # - when PSIP is relaxed, definite targets are applied exactly up until last year for which targets
    #   are specified, then extra capacity can be added freely
    #    - this locks in DistPV forecast and other "definite" construction elements
    #    - this also allows specifying early construction either here or in existing plants tables,
    #      with similar effect
    # - "most-likely" (PBR) targets are listed as "definite" targets, applied when PSIP flag turned off
    # - This module introduces a new treatment of the definite targets compared to the older psip_2012_12:
    #   they are treated as exact targets between the start of the study and the last date specified, but
    #   then more can be added in later years.
    # - Battery_Bulk is cloned as DistBattery and targets are set for that (may be excluded from non-PSIP/PBR scenarios)
    #   - this allows fixed targets for DistBattery in same years as free investment in Battery_Bulk
    # - DistPV and DistBattery are listed as definite targets through 2045
    # - PSIP thermal plants are listed in PSIP targets only
    # - early-years storage and renewables automatically get rebuilt in later years, but we don't consider the
    #   rebuild targets when calculating the fixed-construction period for these technologies, so these are used
    #   as lower limits, not fixed targets.

    # * Alternative strategy (abandoned): start from scratch, modifying gen_predetermined_build
    #   * create input spreadsheet showing forecasted capacity for various technology groups in each zone,
    #     grouped into different adoption forecasts (tech_forecast_scenario)
    #   * store this spreadsheet in a table in the back-end database
    #   * store average cap factor of each project in project table
    #   * scenario_data translates this into construction plans
    #       * rank projects in each technology group by levelized cost
    #       * assign capacity target step-ups first to existing projects, then to lowest-cost project as of that year
    #       * assign reconstruction dates to continue capacity step-ups in later years
    #       * capacity step-downs can't be handled because it's not clear which projects should be retired,
    #         and they may be infeasible; they also don't fit with the idea that these tranches last forever
    #       * write all the construction steps into gen_predetermined_build
    #       * can't create construction plans in import_data because they must avoid rebuilding in occupied
    #         projects, which depends on asset life, which depends on tech_scen_id, not known till scenario_data runs
    #   * this approach could also be used to handle all the existing builds, instead of the current existing projects system
    #   * but we're back to an old problem then -- what about cases where these are floors but not upper limits,
    #     e.g., want to force in one CC plant, but open to having more than that?
    #       * could handle that by moving the predetermined part into a separate project, but then project definitions
    #         must depend on tech_forecast_scenario


    # NOTE: RESOLVE used different wind and solar profiles from Switch.
    # Switch profiles seem to be more accurate, so we optimize against them
    # and show that this may give (small) savings vs. the RESOLVE plan.

    # TODO: Should I use Switch to investigate how much of HECO's poor performance is due
    # to using bad resource profiles (small onshore wind that doesn't rise in the rankings),
    # how much is due to capping PV at 300 MW in 2020,
    # how much is due to non-integrality in RESOLVE (fixed by later jimmying by HECO), and
    # how much is due to forcing in elements before and after the optimization?

    # TODO (maybe): set project-specific targets, so that DistPV targets can be spread among tranches
    # and specific projects in the PSIP can be represented accurately (really just NPM wind). This
    # might also allow reconstruction of exactly the same existing or PSIP project when retired
    # (as specified in the PSIP). Currently the code below lets Switch choose the best project with the
    # same technology when it replaces retired renewable projects.

    # targets for individual generation technologies
    # (year, technology, MW added)
    # For storage technologies with flexible energy value (no
    # gen_storage_energy_to_power_ratio provided), MW added should be replaced
    # by a tuple of (MW, hours).

    # Technologies that are forecasted to be built in "most-likely" scenarios.
    # These apply whenever this module is used, even if rest of PSIP plan is
    # ignored by turning off psip flag. Like PSIP targets, these are assumed
    # to be rebuilt at retirement until the end of the study.
    # NOTE("""
    #     Need to get Switch to model solar+storage using normal storage module;
    #     model AC limit and allow unlimited DC on back side. Then use this to
    #     model RFP PV+BESS and forecasted DGPV+DESS.
    # """)
    tech_group_targets_definite = [
        # installations based on installed capacity used in RESOLVE, as shown in
        # /s/data/HECO Plans/PSIP-WebDAV/2017-01-31 Response to Parties IRs/CA-IR-1/Input and Output Files by Case/E3 and Company Defined Cases/Market DGPV (Reference)/OA_NOLNG/planned_installed_capacities.tab
        # Also see "Market DGPV" forecast in Figure J-10 and Table J-22 of 2016-12-23 PSIP (Vol. 3),
        # which match these levels. Figure J-10 notes that these include FIT
        # projects, so those are modeled separately below.
        # Note: code further below adds in reconstruction of early installations
        (2020, "DistPV", 606.3-444),  # net of 444 installed as of 2016 (in existing generators workbook)
        (2022, "DistPV", 680.3-606.3),
        (2025, "DistPV", 744.9-680.3),
        (2030, "DistPV", 868.7-744.9),
        (2035, "DistPV", 1015.4-868.7),
        (2040, "DistPV", 1163.4-1015.4),
        (2045, "DistPV", 1307.9-1163.4),

        # NOTE: we add together all the different distributed PV programs in
        # Figure J-10, on the assumption that private systems (including those
        # on self-supply tariffs) will only be curtailed at times when the whole
        # system is curtailed, so there's no need to model different private
        # curtailment behavior. This is equivalent to assuming that HECO
        # eventually offers some program to accept power from CSS and SIA
        # systems when the system can use it, instead of forcing curtailment at
        # those times.

        # NOTE: It is unclear from PSIP (p. J-25) whether the forecasted "New Grid
        # Export" program in Fig. J-10 corresponds to the "CGS+" tariff (can
        # export  during day or the "Smart Export" tariff (can only export at
        # night); both were introduced in late 2017
        # https://www.hawaiianelectric.com/documents/products_and_services/customer_renewable_programs/20171020_hawaii_PUC_rooftop_solar_and_storage_press_release.pdf
        # We assume this corresponds to CGS+.

        # Distributed energy storage (DESS) forecasted in PSIP Table J-27, p.
        # J-65, "O'ahu Self-Supply DESS Forecast Cumulative Installed Capacity".
        # PSIP p. G-12 reports that distributed batteries have two hour life,
        # but that seems short for long-term system design, so we use 4 hours.
        (2020, "DistBattery", ((56)/4, 4)),
        (2022, "DistBattery", ((79-56)/4, 4)),
        (2025, "DistBattery", ((108-79)/4, 4)),
        (2030, "DistBattery", ((157-108)/4, 4)),
        (2035, "DistBattery", ((213-157)/4, 4)),
        (2040, "DistBattery", ((264-213)/4, 4)),
        (2045, "DistBattery", ((306-264)/4, 4)),
        # TODO: We could potentially model part of the DESS as being paired with
        # some amount of PV from the CSS pool. (PSIP p. J-25 says distributed
        # energy storage systems (DESS) were paired with DGPV for small
        # customers and sized optimally, but large customers were assumed not to
        # need it because they could take daytime load reductions directly.)
        # However, since PSIP reports that storage sizes were optimized, we
        # assume these batteries are able to serve load as effectively as
        # centralized batteries, so we just model them as generic batteries.

        # NOTE: PSIP p. J-25 says "Additional stand-alone DESS, not necessarily
        # paired with PV, were projected to participate in Demand Response
        # programs". PSIP doesn't show these quantities and they are not in the
        # RESOLVE inputs (the PV-paired DESS weren't in RESOLVE either). We
        # assume these are part of the pool of bulk storage selected by Switch,
        # since they participate on an economic basis.

        # Na Pua Makani (NPM) wind
        # 2018/24 MW in PSIP, but still under construction in late 2019;
        # Reported as 24 MW to be online in 2020 in
        # https://www.hawaiianelectric.com/clean-energy-hawaii/our-clean-energy-portfolio/renewable-project-status-board (accessed 10/22/19)
        # Listed as 27 MW with operation beginning by summer 2020 on https://www.napuamakanihawaii.org/fact-sheet/
        # TODO: Is Na Pua Makani 24 MW or 27 MW?
        (2020, 'OnshoreWind', 27),
        # PSIP 2016: (2018, 'OnshoreWind', 24),

        # HECO feed-in tariff (FIT) projects under construction as of 10/22/19, from
        # https://www.hawaiianelectric.com/clean-energy-hawaii/our-clean-energy-portfolio/renewable-project-status-board
        # NOTE: PSIP Figure J-10 says these are in addition to the customer DGPV
        # adoption forecast, so we model them as standard PV generation projects.
        # TODO: move these to existing-projects tables
        # TODO: model some of these as flat dist PV or utility-scale fixed-slope PV
        (2020, 'LargePV', 5),  # Aloha Solar; actually fixed tilt at 10 degrees, facing south: https://dbedt.hawaii.gov/hcda/files/2017/12/KAL-17-017-ASEF-II-Development-Permit-Application.pdf
        (2020, 'LargePV', 3.5),  # Mauka FIT 1, Kahuku, Tax Map Key (1)5-6-005:014; see PUC docket 2018-0056; can't find info on project geometry (fixed vs tracking), but probably fixed
        # TODO: these weren't in the psip_2016_12 module; were they part of the PSIP DER forecast or did I mistakenly omit them?

        # CBRE wind and PV
        # Final order given allowing HECO to proceed with standardized contracts
        # in June 2018: https://cca.hawaii.gov/dca/files/2018/07/Order-No-35560-HECO-CBRE.pdf
        # "At the ten-month milestone [June 2019], three projects have half-executed standard
        # form contracts ("SFCs") and interconnection agreements." None had subscribers or were
        # under construction at this point.
        # https://dms.puc.hawaii.gov/dms/DocumentViewer?pid=A1001001A19G15A93031F00794
        # In Oct. 2019, HECO's website said it had agreement(s) in place for 4990 kW
        # of the 5000 MW solar allowed in Phase 1, with 330 kW in queue. I think the
        # June 2018 D&O said this will roll over to Phase 2. No mention of wind on
        # the HECO program website.
        # https://www.hawaiianelectric.com/products-and-services/customer-renewable-programs/community-solar
        # According to HECO press release, the first phase includes (only) 8 MW
        # of solar on all islands (5 MW on Oahu). Other techs will be included
        # in phase 2, which will begin "about two years" from 7/2018.
        # https://www.hawaiianelectric.com/regulators-approve-community-solar-plans
        # **** Will CBRE Phase 1 solar enter service in 2020?
        (2020, 'LargePV', 4.990),  # CBRE Phase 1
        # Original CBRE program design had only 72 MW in phase 1 and 2 (leaving
        # 64 MW for phase 2), but HECO suggested increasing this to 235 MW over
        # 5 years. HECO said this was because of projected shortfalls in DER
        # program. Joint Parties say it should be possible to accept all of this
        # earlier and expand the program if it goes quickly, and this should not
        # be used to limit DER adoption.
        # https://dms.puc.hawaii.gov/dms/DocumentViewer?pid=A1001001A19H20B01349C00185
        # **** questions:
        # **** Should we reduce DER forecast in light of HECO's projected shortfall reported in CBRE proceeding?
        # **** How much solar should we expect on Oahu in CBRE Phase 2 and when?
        # **** Do we expect any wind on Oahu in CBRE Phase 2, and if so, when?
        # Until we answer the questions above, this is a placeholder Oahu CBRE Phase 2.
        # This is in addition to RFPs noted below.
        (2022, 'LargePV', 150),  # CBRE Phase 2
        # PSIP 2016: (2018, 'OnshoreWind', 10),
        # PSIP 2016: (2018, 'LargePV', 15),

        # 2018-2019 RFPs (docket 2017-0352)
        # These replace large PV and bulk batteries reported in PSIP for 2020 and 2022.
        # TODO: maybe move these to existing plants tables
        # "On March 25, 2019, the commission approved six ... grid-scale,
        # solar-plus-storage projects.... Cumulatively, the projects will add 247
        # megawatts ("MW") of solar energy with almost 1 gigawatt hour of
        # storage to the HECO Companies' grids."
        # -- D&O 36604, https://dms.puc.hawaii.gov/dms/DocumentViewer?pid=A1001001A19J10A90756F00117
        # First 6 approved projects (dockets 2018-0430, -0431, -0432, -0434, -0435, and -0436) are listed at
        # -- https://www.hawaiianelectric.com/six-low-priced-solar-plus-storage-projects-approved-for-oahu-maui-and-hawaii-islands
        # On 8/20/19, PUC approved 7th project, 12.5 MW/50 MWh AES solar+storage (docket 2019-0050, order 36480)
        # -- https://dms.puc.hawaii.gov/dms/DocumentViewer?pid=A1001001A19H21B03929E00301
        # -- https://www.hawaiianelectric.com/puc-approves-grid-scale-solar-project-in-west-oahu
        # As of 10/22/19, 8th project, 15 MW/60 MWh solar+storage on Maui, is still under review (docket 2018-0433)
        # Status of all approved projects and in-service data are listed at
        # https://www.hawaiianelectric.com/clean-energy-hawaii/our-clean-energy-portfolio/renewable-project-status-board
        (2021, 'LargePV', 12.5), # AES West Oahu Solar
        (2021, 'LargePV', 52), # Hoohana Solar 1
        (2021, 'LargePV', 39), # Mililani I Solar
        (2021, 'LargePV', 36), # Waiawa Solar
        # storage associated with large PV projects; we assume this will be used
        # efficiently, so we model it along with other large-scale storage.
        (2021, 'Battery_Bulk', (12.5+52+39+36, 4)),

        # Placeholder for Oahu portion of RFP Phase 2.
        # Proposals due 11/5/2019 for up to 1,300,000 MWh/year of solar according to
        # https://www.hawaiianelectric.com/clean-energy-hawaii/our-clean-energy-portfolio/renewable-project-status-board
        # avg. cap factor for 560 MW starting after 390 best MW have been installed
        # (existing projects + FIT + CBRE 1 + half of CBRE 2 + RFP 1) is 26.6%; see
        # "select site, max_capacity, avg(cap_factor) from cap_factor natural join project where technology = 'CentralTrackingPV' group by 1, 2 order by 3 desc;"
        # and (120*.271+247*.265+193*.264)/(120+247+193)
        # Then (1,300,000 MWh/y)/(.266 * 8766 h/y) = 558 MW
        (2022, 'LargePV', 560),
        # TODO: will this be only wind or also other technologies?
        # For now, we assume solar-only.
        # TODO: how much storage is anticipated as part of RFP Phase 2?
        # For now, we let Switch choose.

        # PSIP 2016-12-23 Table 4-1 included 90 MW of contingency battery in 2019
        # and https://www.hawaiianelectric.com/documents/clean_energy_hawaii/selling_power_to_the_utility/competitive_bidding/20190207_tri_company_future_procurement.pdf
        # says the 2016-12 plan was to do 70 MW contingency in 2019 and more contingency/regulation in 2020
        # There has been no further discussion of these as of 10/22/19, so we assume they are
        # replaced by storage that comes with the PV systems.
        # PSIP 2016: (2019, 'Battery_Conting', 90),
    ]

    # add targets specified on the command line
    # TODO: allow repeated invocation
    if m.options.force_build is not None:
        b = list(m.options.force_build)
        build = (
            int(b[0]),   # year
            b[1],        # tech
            # quantity
            float(b[2]) if len(b) == 3 else (float(b[2]), float(b[3]))
        )
        print("Forcing build: {}".format(build))
        tech_group_targets_definite.append(build)

    # technologies proposed in PSIP but which may not be built if a better plan is found.
    # All from final plan in Table 4-1 of PSIP 2016-12-23 sometimes cross-referenced with PLEXOS inputs.
    # These differ somewhat from inputs to RESOLVE or the RESOLVE plans in Table 3-1 and 3-4, but
    # they represent HECO's final plan as reported in the PSIP.
    tech_group_targets_psip = [
        (2022, 'IC_Barge', 100.0),         # JBPHH plant
        # note: we moved IC_MCBH one year earlier than PSIP to reduce infeasibility in 2022
        (2022, 'IC_MCBH', 54.0),
        (2025, 'LargePV', 200),
        (2025, 'OffshoreWind', 200),
        (2040, 'LargePV', 280),
        (2045, 'LargePV', 1180),
        (2045, 'IC_MCBH', 68.0), # proxy for 68 MW of generic ICE capacity

        # batteries (MW)
        # from PSIP 2016-12-23 Table 4-1; also see energy ("capacity") and power files in
        # "data/HECO Plans/PSIP-WebDAV/2017-01-31 Response to Parties IRs/DBEDT-IR-12/Input/Oahu/Oahu E3 Plan Input/CSV files/Battery"
        # (note: we mistakenly treated these as MWh quantities instead of MW before 2018-02-20)
        (2025, 'Battery_Bulk', (29, 4)),
        (2030, 'Battery_Bulk', (165, 4)),
        (2035, 'Battery_Bulk', (168, 4)),
        (2040, 'Battery_Bulk', (420, 4)),
        (2045, 'Battery_Bulk', (1525, 4)),
        # RESOLVE modeled 4-hour batteries as being capable of providing reserves,
        # and didn't model contingency batteries (see data/HECO Plans/PSIP-WebDAV/2017-01-31 Response to Parties IRs/CA-IR-1/Input
        # and Output Files by Case/E3 and Company Defined Cases/Market DGPV (Reference)/OA_NOLNG/technologies.tab).
        # Then HECO added a 90 MW contingency battery (table 4-1 of PSIP 2016-12-23).
        # Note: RESOLVE can get reserves from batteries (they only considered 4-hour batteries), but not
        # from EVs or flexible demand.
        # DR: Looking at RESOLVE inputs, it seems like they take roughly 4% of load, and allow it to be doubled
        # or cut to zero each hour (need to double-check this beyond first day). Maybe this includes EVs?
        # (no separate sign of EVs).
        # TODO: check Resolve load levels against Switch.
        # TODO: maybe I should switch over to using the ABC curves and load profiles that HECO used with PLEXOS
        # (for all islands).
        # TODO: Did HECO assume 4-hour batteries, demand response or EVs could provide reserves when running PLEXOS?
        # - all of these seem unlikely, but we have to ask HECO to find out; PLEXOS files are unclear.
    ]

    if psip:
        if m.options.psip_relax_after is not None:
            # NOTE: this could be moved later, if we want this flag to relax
            # both the definite and psip targets
            psip_targets = [t for t in tech_group_targets_psip if t[0] <= m.options.psip_relax_after]
        else:
            psip_targets = tech_group_targets_psip
        tech_group_targets = tech_group_targets_definite + psip_targets
    else:
        tech_group_targets = tech_group_targets_definite

    # Show which technologies can contribute to the target for each technology
    # group and which group each technology contributes to
    techs_for_tech_group = {
        'DistPV': ['DistPV', 'SlopedDistPV', 'FlatDistPV'],
        'LargePV': ['CentralTrackingPV', 'CentralFixedPV'],
    }
    # use the rest as-is
    missing_techs = (
        {t for y, t, s in tech_group_targets}
        .difference(techs_for_tech_group.keys())
    )
    techs_for_tech_group.update({t: [t] for t in missing_techs})
    # create a reverse mapping
    tech_tech_group = {
        tech: tech_group
        for tech_group, techs in techs_for_tech_group.items()
        for tech in techs
    }

    # Rebuild renewable projects and forecasted technologies at retirement.
    # In the future we may be able to simplify this by enforcing capacity targets
    # instead of construction targets.

    # note: this behavior is consistent with the following:
    # discussion on p. 3-8 of PSIP 2016-12-23 vol. 1.
    # Resolve applied planned wind and solar as set levels through 2045, not set additions in each year.
    # Table 4-1 shows final plans that were sent to Plexos; Plexos input files in
    # data/HECO Plans/PSIP-WebDAV/2017-01-31 Response to Parties IRs/DBEDT-IR-12/Input/Oahu/Oahu E3 Plan Input/CSV files/Theme 5
    # show optional capacity built in 2020 or 2025 (in list below) continuing in service in 2045.
    # and Plexos input files in data/HECO Plans/PSIP-WebDAV/2017-01-31 Response to Parties IRs/DBEDT-IR-12/Input/Oahu/Oahu E3 Plan Input/CSV files/PSIP Max Capacity.csv
    # don't show any retirements of wind and solar included as "planned" in RESOLVE and "existing" in Switch
    # (Waivers PV1, West Loch; Kawailoa may be omitted?)
    # also note: Plexos input files in XX
    # show max battery capacity equal to sum of all prior additions

    # m = lambda: 3; m.options = m; m.options.inputs_dir = '/Users/matthias/Dropbox/Research/Ulupono/Enovation Model/pbr_scenario/inputs'
    gen_info = pd.read_csv(os.path.join(m.options.inputs_dir, 'generation_projects_info.csv'))
    gen_info['tech_group'] = gen_info['gen_tech'].map(tech_tech_group)
    gen_info = gen_info[gen_info['tech_group'].notna()]
    # existing technologies are also subject to rebuilding
    existing_techs = (
        pd.read_csv(os.path.join(m.options.inputs_dir, 'gen_build_predetermined.csv'))
        .merge(gen_info, how='inner')
        .groupby(['build_year', 'tech_group'])['gen_predetermined_cap'].sum()
        .reset_index()
    )
    assert not any(is_battery(t) for i, y, t, q in existing_techs.itertuples()), "Must update {} to handle pre-existing batteries.".format(__name__)
    ages = gen_info.groupby('tech_group')['gen_max_age'].agg(['min', 'max', 'mean'])
    assert all(ages['min'] == ages['max']), "Some psip technologies have mixed ages."
    last_period = pd.read_csv(os.path.join(m.options.inputs_dir, 'periods.csv')).iloc[-1, 0]

    # rebuild all renewables and batteries in place before the start of the study,
    # plus any technologies with targets specified here
    rebuildable_targets = [
        (y, t, q)
        for i, y, t, q in existing_techs.itertuples()
        if is_renewable(t) or is_battery(t)
    ] + tech_group_targets
    tech_life = dict()
    for build_year, tech_group, cap in rebuildable_targets:
        if tech_group not in ages.index:
            raise ValueError(
                'A target has been specified for {} but there are no matching '
                'technologies in generation_projects_info.csv.'
                .format(tech_group)
            )
        max_age = ages.loc[tech_group, 'mean']
        tech_life[tech_group] = max_age
        rebuild = 1
        while build_year + rebuild * max_age <= last_period:
            tech_group_targets.append((build_year + rebuild * max_age, tech_group, cap))
            rebuild += 1
    del gen_info, existing_techs, ages, rebuildable_targets

    tech_group_power_targets = [
        (y, t, q[0] if type(q) is tuple else q) for y, t, q in tech_group_targets
    ]
    tech_group_energy_targets = [
        (y, t, q[0]*q[1]) for y, t, q in tech_group_targets if type(q) is tuple
    ]

    m.FORECASTED_TECH_GROUPS = Set(initialize=techs_for_tech_group.keys())
    m.FORECASTED_TECH_GROUP_TECHS = Set(m.FORECASTED_TECH_GROUPS, initialize=techs_for_tech_group)
    m.FORECASTED_TECHS = Set(initialize=tech_tech_group.keys())
    m.tech_tech_group = Param(m.FORECASTED_TECHS, initialize=tech_tech_group)

    # make a list of renewable technologies
    m.RENEWABLE_TECH_GROUPS = Set(
        initialize=m.FORECASTED_TECH_GROUPS,
        filter=lambda m, tg: is_renewable(tg)
    )

    def tech_group_target(m, per, tech, targets):
        """Find the amount of each technology that is targeted to be built
        between the start of the previous period and the start of the current
        period and not yet retired."""
        start = 0 if per == m.PERIODS.first() else m.PERIODS.prev(per)
        end = per
        target = sum(
            q for (tyear, ttech, q) in targets
            if ttech == tech
                and start < tyear and tyear <= end
                and tyear + tech_life[ttech] > end
        )
        return target

    def rule(m, per, tech):
        return tech_group_target(m, per, tech, tech_group_power_targets)
    m.tech_group_power_target = Param(m.PERIODS, m.FORECASTED_TECH_GROUPS, initialize=rule)

    def rule(m, per, tech):
        return tech_group_target(m, per, tech, tech_group_energy_targets)
    m.tech_group_energy_target = Param(m.PERIODS, m.FORECASTED_TECH_GROUPS, initialize=rule)

    def MakeTechGroupDicts_rule(m):
        # get unit sizes of all technologies
        unit_sizes = m.tech_group_unit_size_dict = defaultdict(float)
        for g, unit_size in m.gen_unit_size.items():
            tech = m.gen_tech[g]
            if tech in m.FORECASTED_TECHS:
                tech_group = m.tech_tech_group[tech]
                if tech_group in unit_sizes:
                    if unit_sizes[tech_group] != unit_size:
                        raise ValueError("Generation technology {} uses different unit sizes for different projects.")
                else:
                    unit_sizes[tech_group] = unit_size
        # get predetermined capacity for all technologies
        m.tech_group_predetermined_power_cap_dict = defaultdict(float)
        for (g, per), cap in m.gen_predetermined_cap.items():
            tech = m.gen_tech[g]
            if tech in m.FORECASTED_TECHS:
                tech_group = m.tech_tech_group[tech]
                m.tech_group_predetermined_power_cap_dict[tech_group, per] += cap
        m.tech_group_predetermined_energy_cap_dict = defaultdict(float)
        for (g, per), cap in m.gen_predetermined_cap.items():
            tech = m.gen_tech[g]
            if tech in m.FORECASTED_TECHS and g in m.STORAGE_GENS:
                # Need to get predetermined energy capacity here, but there's no
                # param for it yet, so currently these can only be implemented
                # as technologies with fixed gen_storage_energy_to_power_ratio,
                # in which case users should only provide a power target, not
                # an energy target in this file. In the future, there may be
                # a way to provide predetermined power and energy params, so we
                # watch out for that here.
                if m.gen_storage_energy_to_power_ratio[g] == float("inf"):
                    TODO("Need to lookup predetermined energy capacity for storage technologies.")
                    # m.tech_group_predetermined_energy_cap_dict[tech_group, per] += <predetermined energy cap>
    m.MakeTechGroupDicts = BuildAction(rule=MakeTechGroupDicts_rule)

    # Find last date for which a definite target was specified for each tech group.
    # This sets the last year when construction of a technology is fixed at a
    # predetermined level in the "most-likely" (non-PSIP) cases.
    # This ignores PSIP targets, since _all_ construction is frozen when those are
    # used, and ignores reconstruction targets, because those just follow on from
    # the early-years construction, and we don't want to freeze construction all
    # the way through.
    last_definite_target = dict()
    for y, t, q in tech_group_targets_definite:
        last_definite_target[t] = max(y, last_definite_target.get(t, 0))

    def tech_group_target_rule(m, per, tech_group, build_var, target):
        """
        Enforce targets for each technology.

        with PSIP: build is zero except for tech_group_power_targets
            (sum during each period or before first period)
        without PSIP: build is == definite targets during time range when targets specified
                      build is >= target later;
        Note: in the last case the target is the sum of targets between start of prior period and start of this one
        """
        build = sum(
            build_var[g, per]
            for g in m.GENERATION_PROJECTS
            if m.gen_tech[g] in m.FORECASTED_TECHS
                and m.tech_tech_group[m.gen_tech[g]] == tech_group
                and (g, per) in build_var
        )

        if type(build) is int and build == 0:
            # no matching projects found
            if target == 0:
                return Constraint.Skip
            else:
                raise ValueError(
                    "Target was set for {} in {}, but no matching projects are available."
                    .format(tech_group, per)
                )

        if psip and (m.options.psip_relax_after is None or per <= m.options.psip_relax_after):
            # PSIP in effect: exactly match the target (possibly zero)
            return (build == target)
        elif per <= last_definite_target.get(tech_group, 0):
            # PSIP not in effect, but a definite target is
            return (build == target)
        elif m.options.psip_minimal_renewables and tech_group in m.RENEWABLE_TECH_GROUPS:
            # Only build the specified amount of renewables, no more.
            # This is used to apply the definite targets, but otherwise minimize renewable development.
            return (build == target)
        else:
            # treat the target as a lower bound
            return (build >= target)

    def rule(m, per, tech_group):
        # get target, including any capacity specified in the predetermined builds,
        # so the target will be additional to those
        target = m.tech_group_power_target[per, tech_group] + m.tech_group_predetermined_power_cap_dict[tech_group, per]
        return tech_group_target_rule(m, per, tech_group, m.BuildGen, target)
    m.Enforce_Tech_Group_Power_Target = Constraint(
        m.PERIODS, m.FORECASTED_TECH_GROUPS, rule=rule
    )
    def rule(m, per, tech_group):
        # get target, including any capacity specified in the predetermined builds,
        # so the target will be additional to those
        target = m.tech_group_energy_target[per, tech_group] + m.tech_group_predetermined_energy_cap_dict[tech_group, per]
        return tech_group_target_rule(m, per, tech_group, m.BuildStorageEnergy, target)
    m.Enforce_Tech_Group_Energy_Target = Constraint(
        m.PERIODS, m.FORECASTED_TECH_GROUPS, rule=rule
    )

    if psip:
        TODO("""
            Need to force construction to zero for technologies without targets
            in the PSIP.
        """)
        # don't allow construction of other technologies (e.g., pumped hydro, fuel cells)
        advanced_tech_vars = [
            "BuildPumpedHydroMW", "BuildAnyPumpedHydro",
            "BuildElectrolyzerMW", "BuildLiquifierKgPerHour", "BuildLiquidHydrogenTankKg",
            "BuildFuelCellMW",
        ]
        def no_advanced_tech_rule_factory(v):
            return lambda m, *k: (getattr(m, v)[k] == 0)
        for v in advanced_tech_vars:
            try:
                var = getattr(m, v)
                setattr(m, "PSIP_No_"+v, Constraint(var._index, rule=no_advanced_tech_rule_factory(v)))
            except AttributeError:
                pass    # model doesn't have this var