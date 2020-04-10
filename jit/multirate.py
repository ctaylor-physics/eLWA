"""
Module that provide the multi-rate F-engine needed to correlate data at 
different sample rates.
"""

# Python3 compatibility
from __future__ import print_function, division, absolute_import
import sys
if sys.version_info > (3,):
    xrange = range
    
import ephem
import numpy

from astropy.constants import c as vLight
vLight = vLight.to('m/s').value

from lsl.common import dp as dp_common
from lsl.correlator import _core
from lsl.correlator.fx import pol_to_pol, null_window

from jit import JustInTimeOptimizer

__version__ = '0.2'
__all__ = ['get_optimal_delay_padding', 'fengine', 'pfbengine', 'xengine', 'xengine3']


JIT_OPT = JustInTimeOptimizer()


def get_optimal_delay_padding(antennaSet1, antennaSet2, LFFT=64, SampleRate=None, CentralFreq=0.0, Pol='XX', phaseCenter='z'):
    # Decode the polarization product into something that we can use to figure 
    # out which antennas to use for the cross-correlation
    if Pol == '*':
        antennas1 = antennaSet1
        antennas2 = antennaSet2
        
    else:
        pol1, pol2 = pol_to_pol(Pol)
        
        antennas1 = [a for a in antennaSet1 if a.pol == pol1]
        antennas2 = [a for a in antennaSet2 if a.pol == pol1]
        
    # Combine the two sets and proceede
    antennas1.extend(antennas2)
    nStands = len(antennas1)
    
    # Create a reasonable mock setup for computing the delays
    if SampleRate is None:
        SampleRate = dp_common.fS
    freq = numpy.fft.fftfreq(LFFT, d=1.0/SampleRate)
    freq += float(CentralFreq)
    freq = numpy.fft.fftshift(freq)
    
    # Get the location of the phase center in radians and create a 
    # pointing vector
    if phaseCenter == 'z':
        azPC = 0.0
        elPC = numpy.pi/2.0
    else:
        if isinstance(phaseCenter, ephem.Body):
            azPC = phaseCenter.az * 1.0
            elPC = phaseCenter.alt * 1.0
        else:
            azPC = phaseCenter[0]*numpy.pi/180.0
            elPC = phaseCenter[1]*numpy.pi/180.0
            
    source = numpy.array([numpy.cos(elPC)*numpy.sin(azPC), 
                    numpy.cos(elPC)*numpy.cos(azPC), 
                    numpy.sin(elPC)])
                    
    # Define the cable/signal delay caches to help correlate along and compute 
    # the delays that we need to apply to align the signals
    dlyRef = len(freq)/2
    delays1 = numpy.zeros((nStands,LFFT))
    for i in list(range(nStands)):
        xyz1 = numpy.array([antennas1[i].stand.x, antennas1[i].stand.y, antennas1[i].stand.z])
        
        delays1[i,:] = antennas1[i].cable.delay(freq) - numpy.dot(source, xyz1) / vLight
    minDelay = delays1[:,dlyRef].min()
    
    # Round to the next lowest 5 us, negate, and return
    minDelay = numpy.floor( minDelay / 5e-6) * 5e-6
    return -minDelay


def fengine(signals, antennas, LFFT=64, overlap=1, IncludeAuto=False, verbose=False, window=null_window, SampleRate=None, CentralFreq=0.0, Pol='XX', GainCorrect=False, ReturnBaselines=False, ClipLevel=0, phaseCenter='z', delayPadding=40e-6):
    """
    Multi-rate F engine based on the lsl.correlator.fx.FXMaster() function.
    """
    
    # Decode the polarization product into something that we can use to figure 
    # out which antennas to use for the cross-correlation
    if Pol == '*':
        antennas1 = antennas
        signalsIndex1 = [i for (i, a) in enumerate(antennas)]
        
    else:
        pol1, pol2 = pol_to_pol(Pol)
        
        antennas1 = [a for a in antennas if a.pol == pol1]
        signalsIndex1 = [i for (i, a) in enumerate(antennas) if a.pol == pol1]
    
    nStands = len(antennas1)
    
    # Figure out if we are working with complex (I/Q) data or only real.  This
    # will determine how the FFTs are done since the real data mirrors the pos-
    # itive and negative Fourier frequencies.
    if signals.dtype.kind == 'c':
        lFactor = 1
        doFFTShift = True
        CentralFreq = float(CentralFreq)
    else:
        lFactor = 2
        doFFTShift = False
        
    if SampleRate is None:
        SampleRate = dp_common.fS
    freq = numpy.fft.fftfreq(lFactor*LFFT, d=1.0/SampleRate) + CentralFreq
    if doFFTShift:
        freq = numpy.fft.fftshift(freq)
    freq = freq[:LFFT]
    
    # Get the location of the phase center in radians and create a 
    # pointing vector
    if phaseCenter == 'z':
        azPC = 0.0
        elPC = numpy.pi/2.0
    else:
        if isinstance(phaseCenter, ephem.Body):
            azPC = phaseCenter.az * 1.0
            elPC = phaseCenter.alt * 1.0
        else:
            azPC = phaseCenter[0]*numpy.pi/180.0
            elPC = phaseCenter[1]*numpy.pi/180.0
            
    source = numpy.array([numpy.cos(elPC)*numpy.sin(azPC), 
                    numpy.cos(elPC)*numpy.cos(azPC), 
                    numpy.sin(elPC)])
                    
    # Define the cable/signal delay caches to help correlate along and compute 
    # the delays that we need to apply to align the signals
    dlyRef = len(freq)/2
    delays1 = numpy.zeros((nStands,LFFT))
    for i in list(range(nStands)):
        xyz1 = numpy.array([antennas1[i].stand.x, antennas1[i].stand.y, antennas1[i].stand.z])
        
        delays1[i,:] = antennas1[i].cable.delay(freq) - numpy.dot(source, xyz1) / vLight + delayPadding
    minDelay = delays1[:,dlyRef].min()
    if minDelay < 0:
        raise RuntimeError('Minimum data stream delay is negative: %.3f us' % (minDelay*1e6,))
        
    # Optimize
    if len(signalsIndex1) != signals.shape[0]:
        FEngine = JIT_OPT.get_function(_core.FEngineC2, signals[signalsIndex1,:], freq, delays1, LFFT=LFFT, overlap=overlap, SampleRate=SampleRate, ClipLevel=ClipLevel)
    else:
        FEngine = JIT_OPT.get_function(_core.FEngineC2, signals, freq, delays1, LFFT=LFFT, overlap=overlap, SampleRate=SampleRate, ClipLevel=ClipLevel)
    
    # F - defaults to running parallel in C via OpenMP
    if len(signalsIndex1) != signals.shape[0]:
        signalsF1, validF1 = FEngine(signals[signalsIndex1,:], freq, delays1, SampleRate=SampleRate)
    else:
        signalsF1, validF1 = FEngine(signals, freq, delays1, SampleRate=SampleRate)
    
    return freq, signalsF1, validF1, delays1


def pfbengine(signals, antennas, LFFT=64, overlap=1, IncludeAuto=False, verbose=False, window=null_window, SampleRate=None, CentralFreq=0.0, Pol='XX', GainCorrect=False, ReturnBaselines=False, ClipLevel=0, phaseCenter='z', delayPadding=40e-6):
    """
    Multi-rate PFB-based F engine based on the lsl.correlator.fx.FXMaster() function.
    """
    
    # Decode the polarization product into something that we can use to figure 
    # out which antennas to use for the cross-correlation
    if Pol == '*':
        antennas1 = antennas
        signalsIndex1 = [i for (i, a) in enumerate(antennas)]
        
    else:
        pol1, pol2 = pol_to_pol(Pol)
        
        antennas1 = [a for a in antennas if a.pol == pol1]
        signalsIndex1 = [i for (i, a) in enumerate(antennas) if a.pol == pol1]
    
    nStands = len(antennas1)
    
    # Figure out if we are working with complex (I/Q) data or only real.  This
    # will determine how the FFTs are done since the real data mirrors the pos-
    # itive and negative Fourier frequencies.
    if signals.dtype.kind == 'c':
        lFactor = 1
        doFFTShift = True
        CentralFreq = float(CentralFreq)
    else:
        lFactor = 2
        doFFTShift = False
        
    if SampleRate is None:
        SampleRate = dp_common.fS
    freq = numpy.fft.fftfreq(lFactor*LFFT, d=1.0/SampleRate) + CentralFreq
    if doFFTShift:
        freq = numpy.fft.fftshift(freq)
    freq = freq[:LFFT]
    
    # Get the location of the phase center in radians and create a 
    # pointing vector
    if phaseCenter == 'z':
        azPC = 0.0
        elPC = numpy.pi/2.0
    else:
        if isinstance(phaseCenter, ephem.Body):
            azPC = phaseCenter.az * 1.0
            elPC = phaseCenter.alt * 1.0
        else:
            azPC = phaseCenter[0]*numpy.pi/180.0
            elPC = phaseCenter[1]*numpy.pi/180.0
            
    source = numpy.array([numpy.cos(elPC)*numpy.sin(azPC), 
                    numpy.cos(elPC)*numpy.cos(azPC), 
                    numpy.sin(elPC)])
                    
    # Define the cable/signal delay caches to help correlate along and compute 
    # the delays that we need to apply to align the signals
    dlyRef = len(freq)/2
    delays1 = numpy.zeros((nStands,LFFT))
    for i in list(range(nStands)):
        xyz1 = numpy.array([antennas1[i].stand.x, antennas1[i].stand.y, antennas1[i].stand.z])
        
        delays1[i,:] = antennas1[i].cable.delay(freq) - numpy.dot(source, xyz1) / vLight + delayPadding
    minDelay = delays1[:,dlyRef].min()
    if minDelay < 0:
        raise RuntimeError('Minimum data stream delay is negative: %.3f us' % (minDelay*1e6,))
        
    # Optimize
    if len(signalsIndex1) != signals.shape[0]:
        FEngine = JIT_OPT.get_function('PFBEngine', signals[signalsIndex1,:], freq, delays1, LFFT=LFFT, overlap=overlap, SampleRate=SampleRate, ClipLevel=ClipLevel)
    else:
        FEngine = JIT_OPT.get_function('PFBEngine', signals, freq, delays1, LFFT=LFFT, overlap=overlap, SampleRate=SampleRate, ClipLevel=ClipLevel)
        
    # F - defaults to running parallel in C via OpenMP
    if len(signalsIndex1) != signals.shape[0]:
        signalsF1, validF1 = FEngine(signals[signalsIndex1,:], freq, delays1, SampleRate=SampleRate)
    else:
        signalsF1, validF1 = FEngine(signals, freq, delays1, SampleRate=SampleRate)
    
    return freq, signalsF1, validF1, delays1


def xengine(signalsF1, validF1, signalsF2, validF2):
    """
    X-engine for the outputs of fengine().
    """
    
    # Optimize
    #XEngine = JIT_OPT.get_function('XEngine2', signalsF1, signalsF2, validF1, validF2)
    XEngine = _core.XEngine2

    output = XEngine(signalsF1, signalsF2, validF1, validF2)
    return output


def xengine3(signalsF1, validF1, signalsF2, validF2):
    """
    X-engine for the outputs of fengine().
    """
    
    # Optimize
    XEngine = JIT_OPT.get_function('XEngine3', signalsF1, signalsF2, validF1, validF2)

    output = XEngine(signalsF1, signalsF2, validF1, validF2)
    return output
