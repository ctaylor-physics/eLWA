#!/usr/bin/env python

"""
RFI flagger for FITS-IDI files containing eLWA data.

$Rev$
$LastChangedBy$
$LastChangedDate$
"""

import os
import sys
import time
import numpy
import pyfits
import argparse
from datetime import datetime

from lsl.astro import utcjd_to_unix

from flagger import *


def main(args):
    # Parse the command line
    filenames = args.filename
    
    for filename in filenames:
        t0 = time.time()
        print "Working on '%s'" % os.path.basename(filename)
        # Open the FITS IDI file and access the UV_DATA extension
        hdulist = pyfits.open(filename, mode='readonly')
        andata = hdulist['ANTENNA']
        fqdata = hdulist['FREQUENCY']
        fgdata = None
        for hdu in hdulist[1:]:
            if hdu.header['EXTNAME'] == 'FLAG':
                fgdata = hdu
        uvdata = hdulist['UV_DATA']
        
        # Verify we can flag this data
        if uvdata.header['STK_1'] > 0:
            raise RuntimeError("Cannot flag data with STK_1 = %i" % uvdata.header['STK_1'])
        if uvdata.header['NO_STKD'] < 4:
            raise RuntimeError("Cannot flag data with NO_STKD = %i" % uvdata.header['NO_STKD'])
            
        # Pull out various bits of information we need to flag the file
        ## Antenna look-up table
        antLookup = {}
        for an, ai in zip(andata.data['ANNAME'], andata.data['ANTENNA_NO']):
            antLookup[an] = ai
        ## Frequency and polarization setup
        nBand, nFreq, nStk = uvdata.header['NO_BAND'], uvdata.header['NO_CHAN'], uvdata.header['NO_STKD']
        ## Baseline list
        bls = uvdata.data['BASELINE']
        ## Time of each integration
        obsdates = uvdata.data['DATE']
        obstimes = uvdata.data['TIME']
        ## Source list
        srcs = uvdata.data['SOURCE']
        ## Band information
        fqoffsets = fqdata.data['BANDFREQ'].ravel()
        ## Frequency channels
        freq = (numpy.arange(nFreq)-(uvdata.header['CRPIX3']-1))*uvdata.header['CDELT3']
        freq += uvdata.header['CRVAL3']
        ## UVW coordinates
        u, v, w = uvdata.data['UU'], uvdata.data['VV'], uvdata.data['WW']
        uvw = numpy.array([u, v, w]).T
        ## The actual visibility data
        flux = uvdata.data['FLUX'].astype(numpy.float32)
        ## History to look for delay steps
        delaysteps = []
        try:
            for entry in hdulist[0].header['HISTORY']:
                if entry[:13] == 'Delay step at':
                    delaysteps.append( float(entry.rsplit(None, 1)[1]) )
        except KeyError:
            pass
        if len(delaysteps) == 0:
            print "  No delay step information found, skipping"
            hdulist.close()
            continue
            
        # Convert the masks into a format suitable for writing to a FLAG table
        print "  Building FLAG table"
        ants, times, bands, chans, pols, reas, sevs = [], [], [], [], [], [], []
        ## Old flags
        if fgdata is not None:
            for row in fgdata.data:
                ants.append( row['ANTS'] )
                times.append( row['TIMERANG'] )
                bands.append( row['BANDS'] )
                chans.append( row['CHANS'] )
                pols.append( row['PFLAGS'] )
                reas.append( row['REASON'] )
                sevs.append( row['SEVERITY'] )
        
        # Add in the delay step flags for the LWA baselines
        for step in delaysteps:
            tStart = astro.unix_to_utcjd(step) - obsdates[0]
            tStop = astro.unix_to_utcjd(step+1.0) - obsdates[0]
            for an in antLookup:
                ant1 = antLookup[an]
                if ant1 > 50:
                    ants.append( (ant1,0) )
                    times.append( (tStart, tStop) )
                    bands.append( [1 for j in xrange(nBand)] )
                    chans.append( (0, 0) )
                    pols.append( (1, 1, 1, 1) )
                    reas.append( 'FLAGDELAYSTEP.PY' )
                    sevs.append( -1 )
                    
        ## Build the FLAG table
        print '    FITS HDU'
        ### Columns
        nFlags = len(ants)
        c1 = pyfits.Column(name='SOURCE_ID', format='1J',           array=numpy.zeros((nFlags,), dtype=numpy.int32))
        c2 = pyfits.Column(name='ARRAY',     format='1J',           array=numpy.zeros((nFlags,), dtype=numpy.int32))
        c3 = pyfits.Column(name='ANTS',      format='2J',           array=numpy.array(ants, dtype=numpy.int32))
        c4 = pyfits.Column(name='FREQID',    format='1J',           array=numpy.zeros((nFlags,), dtype=numpy.int32))
        c5 = pyfits.Column(name='TIMERANG',  format='2E',           array=numpy.array(times, dtype=numpy.float32))
        c6 = pyfits.Column(name='BANDS',     format='%iJ' % nBand,  array=numpy.array(bands, dtype=numpy.int32).squeeze())
        c7 = pyfits.Column(name='CHANS',     format='2J',           array=numpy.array(chans, dtype=numpy.int32))
        c8 = pyfits.Column(name='PFLAGS',    format='4J',           array=numpy.array(pols, dtype=numpy.int32))
        c9 = pyfits.Column(name='REASON',    format='A40',          array=numpy.array(reas))
        c10 = pyfits.Column(name='SEVERITY', format='1J',           array=numpy.array(sevs, dtype=numpy.int32))
        colDefs = pyfits.ColDefs([c1, c2, c3, c4, c5, c6, c7, c8, c9, c10])
        ### The table itself
        flags = pyfits.new_table(colDefs)
        ### The header
        flags.header['EXTNAME'] = ('FLAG', 'FITS-IDI table name')
        flags.header['EXTVER'] = (1 if fgdata is None else fgdata.header['EXTVER']+1, 'table instance number') 
        flags.header['TABREV'] = (2, 'table format revision number')
        for key in ('NO_STKD', 'STK_1', 'NO_BAND', 'NO_CHAN', 'REF_FREQ', 'CHAN_BW', 'REF_PIXL', 'OBSCODE', 'ARRNAM', 'RDATE'):
            flags.header[key] = (uvdata.header[key], uvdata.header.comments[key])
        flags.header['HISTORY'] = 'Flagged with %s, revision $Rev$' % os.path.basename(__file__)
        if args.sdm is not None:
            flags.header['HISTORY'] = 'SDM flags from %s' % os.path.basename(os.path.abspath(args.sdm))
        flags.header['HISTORY'] = '%i spurious correlation passes used' % args.scf_passes
        
        # Insert the new table right before UV_DATA
        hdulist.insert(-1, flags)
        
        # Save
        print "  Saving to disk"
        ## What to call it
        outname = os.path.basename(filename)
        outname, outext = os.path.splitext(outname)
        outname = '%s_flagged%s' % (outname, outext)
        ## Does it already exist or not
        if os.path.exists(outname):
            if not args.force:
                yn = raw_input("WARNING: '%s' exists, overwrite? [Y/n] " % outname)
            else:
                yn = 'y'
                
            if yn not in ('n', 'N'):
                os.unlink(outname)
            else:
                raise RuntimeError("Output file '%s' already exists" % outname)
        ## Open and create a new primary HDU
        hdulist2 = pyfits.open(outname, mode='append')
        primary =	pyfits.PrimaryHDU()
        for key in hdulist[0].header:
            primary.header[key] = (hdulist[0].header[key], hdulist[0].header.comments[key])
        hdulist2.append(primary)
        hdulist2.flush()
        ## Copy the extensions over to the new file
        for hdu in hdulist[1:]:
            hdulist2.append(hdu)
            hdulist2.flush()
        hdulist2.close()
        hdulist.close()
        print "  -> Flagged FITS IDI file is '%s'" % outname
        print "  Finished in %.3f s" % (time.time()-t0,)


if __name__ == "__main__":
    numpy.seterr(all='ignore')
    parser = argparse.ArgumentParser(
        description='Flag RFI in FITS-IDI files', 
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('filename', type=str, nargs='+', 
                        help='filename to process')
    parser.add_argument('-s', '--sdm', type=str, 
                        help='read in the provided VLA SDM for additional flags')
    parser.add_argument('-p', '--scf-passes', type=int, default=0, 
                        help='number of passes to make through the spurious correlation sub-routine')
    parser.add_argument('-f', '--force', action='store_true', 
                        help='force overwriting of existing FITS-IDI files')
    args = parser.parse_args()
    main(args)
    