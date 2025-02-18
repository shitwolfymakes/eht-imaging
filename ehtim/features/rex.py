# rex.py
# ring fitting code for ehtim
#
#    Copyright (C) 2019 Andrew Chael
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import division
from __future__ import print_function

from builtins import str
from builtins import range
from builtins import object

import os
import glob
import matplotlib.pyplot as plt
import numpy as np
import astropy.io.fits as fits
import subprocess

import scipy.interpolate
import scipy.optimize
import scipy.stats
from astropy.stats import median_absolute_deviation

from ehtim.image import load_image
import ehtim.imaging.dynamical_imaging as di
import ehtim.parloop as ploop
import ehtim.const_def as ehc

###################################################################################################
# Parameters
###################################################################################################

EP = 1.e-16
BIG = 1./EP

IMSIZE = 160*ehc.RADPERUAS  # 250*ehc.RADPERUAS  # FOV of resampled image (muas)
NPIX = 160  # 128             # pixels in resampled image

NRAYS = 360       # number of angular rays in final profile
NRS = 100         # number of radial points in final profile

RMAX = 50               # maximum radius in every profile slice (muas)
RMIN = 5         # radius threshold for averaging inside ring (muas)

RPRIOR_MIN = 15.  # 5.   # minimum radius for search (muas)
RPRIOR_MAX = 50.  # 60.  # maximum radius for search (muas)
NRAYS_SEARCH = 25  # number of angular rays in search profiles
NRS_SEARCH = 50   # number of radial points in search profiles
THRESH = 0.05     # thresholding level for the images in the search
# BLUR_VALUE_MIN=2  # blur to this value for initial centroid search (uas)
FOVP_SEARCH = 0.1  # fractional FOV around image center for brute force search
NSEARCH = 10      # number of points in each dimension for brute force search
NORMFLUX = 1      # normalized image flux for outputted profiles (Jy)

POSTPROCDIR = '.'  # default postprocessing directory

###################################################################################################
# Profiles class
###################################################################################################


class Profiles(object):

    def __init__(self, im, x0, y0, profs, thetas, rmin=RMIN, rmax=RMAX, flux_norm=NORMFLUX,
                 profsQ=[], profsU=[]):

        self.x0 = x0
        self.y0 = y0
        self.im = im
        self.rmin = rmin
        self.rmax = rmax

        # store the center image
        deltay = -(im.fovy()/2. - y0*ehc.RADPERUAS)/im.psize
        deltax = (im.fovx()/2. - x0*ehc.RADPERUAS)/im.psize
        self.im_center = im.shift([int(np.round(deltay)), int(np.round(deltax))])

        # total flux and normalization
        self.flux = im.total_flux()
        self.parea = (im.psize/ehc.RADPERUAS)**2

        # factor to convert to normalized brightness temperature (total flux of 1 Jy)
        self.flux_norm = flux_norm
        self.normfactor = self.flux_norm / im.total_flux()

        # image array and  profiles
        factor = 3.254e13/(im.rf**2 * im.psize**2)  # factor to convert to brightness temperature
        self.imarr = im.imvec.reshape(im.ydim, im.xdim)[::-1] * factor  # in Tb

        self.xs = np.arange(im.xdim)*im.psize/ehc.RADPERUAS
        self.ys = np.arange(im.ydim)*im.psize/ehc.RADPERUAS
        self.interp = scipy.interpolate.interp2d(self.ys, self.xs, self.imarr, kind='cubic')

        self.profiles = np.array(profs)
        self.profilesQ = np.array(profsQ)
        self.profilesU = np.array(profsU)
        self.profilesP = np.sqrt(self.profilesQ**2 + self.profilesU**2)
        self.thetas = np.array(thetas)
        self.nang = len(thetas)
        self.nrs = len(self.profiles[0])
        self.nthetas = len(self.thetas)
        self.rs = np.linspace(0, self.rmax, self.nrs)
        self.dr = self.rs[-1] - self.rs[-2]
        self.pks = []
        self.pk_vals = []
        self.diameters = []

        for prof in self.profiles:
            pk, vpk = self.calc_pkrad_from_prof(prof)

            self.pks.append(pk)
            self.pk_vals.append(vpk)
            self.diameters.append(2*np.abs(pk))

        self.pks = np.array(self.pks)
        self.pk_vals = np.array(self.pk_vals)
        self.diameters = np.array(self.diameters)

        # ring size
        self.RingSize1 = (np.mean(self.diameters), np.std(self.diameters))
        self.RingSize1_med = (np.median(self.diameters), median_absolute_deviation(self.diameters))

    def calc_pkrad_from_prof(self, prof):
        """calculate peak radius and value with linear interpolation"""
        args = np.argsort(prof)
        pkpos = args[-1]
        pk = self.rs[pkpos]
        vpk = prof[pkpos]
        if pkpos > 0 and pkpos < self.nrs-1:
            vals = [prof[pkpos-1], prof[pkpos], prof[pkpos+1]]
            pk, vpk = quad_interp_radius(pk, self.dr, vals)
        return (pk, vpk)

    def calc_meanprof_and_stats(self):

        # calculate mean profile
        self.meanprof = np.mean(self.profiles, axis=0)
        args = np.argsort(self.meanprof)

        self.pkloc = args[-1]
        self.pkrad = self.rs[self.pkloc]
        self.meanpk = self.meanprof[self.pkloc]

        # absolute peak in angle and radius
        profile_peak_loc = np.unravel_index(np.argmax(self.profiles), self.profiles.shape)
        self.abspk_loc_rad = profile_peak_loc[1]
        self.abspk_rad = self.rs[self.abspk_loc_rad]
        self.abspk_loc_ang = profile_peak_loc[0]
        self.abspk_ang = self.thetas[self.abspk_loc_ang]

        # find inside mean flux
        inner_loc = np.argmin((self.rs-self.rmin)**2)
        self.in_level = np.mean(self.meanprof[0:inner_loc])  # profile avg inside ring

        # find outside mean flux
        outer_loc = np.argmin((self.rs-(self.rmax-self.rmin))**2)
        self.out_level = np.mean(self.meanprof[outer_loc:])  # profile avg outside ring

        # find mean profile FWHM with spline interpolation
        meanprof_zeroed = self.meanprof - self.out_level
        (lh_meanprof, rh_meanprof) = self.calc_width(meanprof_zeroed)
        lhloc_meanprof = np.argmin((self.rs-lh_meanprof)**2)
        rhloc_meanprof = np.argmin((self.rs-rh_meanprof)**2)

        self.lh = lh_meanprof
        self.rh = rh_meanprof
        self.lhloc = lhloc_meanprof
        self.rhloc = rhloc_meanprof

        # ring diameter and  width from the  mean profile
        meanprof_diameter = 2*self.pkrad
        meanprof_width = np.abs(rh_meanprof - lh_meanprof)
        self.RingSize2 = (meanprof_diameter, meanprof_width)

        # find ring width with all angular profiles
        ringwidths = []
        for i in range(self.nang):
            rprof = self.profiles[i]
            # TODO zero min profile before taking width???
            # rprof_zeroed = rprof - np.max((np.min(rprof), 0))
            (lh,  rh) = self.calc_width(rprof)
            width = rh-lh
            if width <= 0 or width >= 2*meanprof_width:
                continue  # AC ?? ok to exclude huge widths???
            ringwidths.append(width)

        self.RingWidth = (np.mean(ringwidths), np.std(ringwidths))

        # ring angle 1: mean and std deviation of individual profiles
        ringangles = []
        ringasyms = []
        for i in range(self.lhloc, self.rhloc+1):
            angprof = self.profiles.T[i]
            if i == self.lhloc:
                prof_mean_r = angprof.reshape(1, len(self.profiles.T[i]))
            else:
                prof_mean_r = np.vstack((prof_mean_r, angprof))

            angle_asym = self.calc_ringangle_asymmetry(angprof)
            ringangles.append(angle_asym[0])
            ringasyms.append(angle_asym[1])

        self.RingAngle1 = (scipy.stats.circmean(ringangles), scipy.stats.circstd(ringangles))

        # ring angle 2: ring angle function on avg  profile
        prof_mean_r = np.mean(np.array(prof_mean_r), axis=0)
        self.meanprof_theta = prof_mean_r
        ringangle2 = self.calc_ringangle_asymmetry(prof_mean_r)
        self.RingAngle2 = (ringangle2[0], ringangle2[-1])

        # contrast 1: maximum  profile value / mean of inner region
        # self.RingContrast1 = np.max(self.profiles.T[self.pkloc]) / self.in_level
        # self.RingContrast1 = np.max(self.profiles) / self.in_level
        self.RingContrast1 = np.max(self.profiles[:, self.lhloc:self.rhloc+1]) / self.in_level

        # contrast 1: mean profile max value / mean of inner region
        self.RingContrast2 = self.meanpk / self.in_level

        # asymmetry 1: m1 mode of angular profile
        self.RingAsym1 = (np.mean(ringasyms), np.std(ringasyms))

        # asymmetry 2: integrated flux in bottom half of ring vs top half of ring
        mask_inner = self.im.copy()
        mask_outer = self.im.copy()
        immask = self.im.copy()

        x0_c = self.im.fovx()/2. - self.x0*ehc.RADPERUAS
        y0_c = self.y0*ehc.RADPERUAS - self.im.fovy()/2.

        # mask annulus
        rad_inner = (self.RingSize1[0]/2. - self.RingWidth[0]/2.)*ehc.RADPERUAS
        rad_outer = (self.RingSize1[0]/2. + self.RingWidth[0]/2.)*ehc.RADPERUAS

        mask_inner.imvec *= 0
        mask_outer.imvec *= 0
        mask_inner = mask_inner.add_gauss(1, [2*rad_inner, 2*rad_inner, 0, x0_c, y0_c])
        mask_inner = mask_inner.mask(cutoff=.5)
        mask_outer = mask_outer.add_gauss(1, [2*rad_outer, 2*rad_outer, 0, x0_c, y0_c])
        mask_outer = mask_outer.mask(cutoff=.5)

        maskvec_annulus = np.logical_xor(
            mask_inner.imvec.astype(bool), mask_outer.imvec.astype(bool))

        # mask angle
        xlist = np.arange(0, -self.im.xdim, -1)*self.im.psize + \
            (self.im.psize*self.im.xdim)/2.0 - self.im.psize/2.0
        ylist = np.arange(0, -self.im.ydim, -1)*self.im.psize + \
            (self.im.psize*self.im.ydim)/2.0 - self.im.psize/2.0

        cangle = self.RingAngle1[0]

        def anglemask(x, y):
            ang = np.mod(-np.arctan2(y-y0_c, x-x0_c)+np.pi/2., 2*np.pi)
            # return ang
            if np.mod(np.abs(ang-cangle), 2*np.pi) > 0.5*np.pi:
                return False
            else:
                return True

        maskvec_ang = np.array([[anglemask(i, j) for i in xlist]
                                for j in ylist]).flatten().astype(bool)

        # combine masks and get the bright and dim flux
        maskvec_brighthalf = maskvec_annulus * maskvec_ang
        brightflux = np.sum(immask.imvec[(maskvec_brighthalf)])

        maskvec_dimhalf = maskvec_annulus * ~maskvec_ang
        dimflux = np.sum(immask.imvec[(maskvec_dimhalf)])
        self.RingFlux = brightflux + dimflux
        self.RingAsym2 = ((brightflux-dimflux)/(brightflux+dimflux), brightflux/dimflux)

        # Polarization brightness ratio
        # AC TODO FOR PAPER VIII ANALYSIS
        self.RingAsymPol = (0., 0.)
        if len(self.im.qvec) > 0 and len(self.im.uvec) > 0:
            pvec = np.sqrt(self.im.qvec**2 + self.im.uvec**2)
            pvec_C = (self.im.qvec + 1j*self.im.uvec)

            ringanglesPol = []
            # ringasymsPol = []
            for i in range(self.lhloc, self.rhloc+1):
                angprof = self.profilesP.T[i]
                # simple maximum AC TODO
                ringanglesPol.append(self.thetas[np.argmax(angprof)])

                # weighted avg
                # angle_asym = self.calc_ringangle_asymmetry(angprof)
                # ringanglesPol.append(angle_asym[0])
                # ringasymsPol.append(angle_asym[1])

            self.RingAnglePol = (scipy.stats.circmean(ringanglesPol),
                                 scipy.stats.circstd(ringanglesPol))

            cangle = self.RingAnglePol[0]

            def anglemask_pol(x, y):
                ang = np.mod(-np.arctan2(y-y0_c, x-x0_c)+np.pi/2., 2*np.pi)
                # return ang
                if np.mod(np.abs(ang-cangle), 2*np.pi) > 0.5*np.pi:
                    return False
                else:
                    return True

            maskvec_ang = np.array([[anglemask_pol(i, j) for i in xlist]
                                    for j in ylist]).flatten().astype(bool)

            # combine masks and get the bright and dim pol flux
            maskvec_brighthalf = maskvec_annulus * maskvec_ang
            maskvec_dimhalf = maskvec_annulus * ~maskvec_ang
            # maskvec_brighthalf = maskvec_ang
            # maskvec_dimhalf = ~maskvec_ang

            # calculate polarized asymmetry /  birghtness ratio
            brightflux_pol_C = np.abs(np.sum(pvec_C[(maskvec_brighthalf)]))
            dimflux_pol_C = np.abs(np.sum(pvec_C[(maskvec_dimhalf)]))
            brightflux_pol = np.sum(pvec[(maskvec_brighthalf)])
            dimflux_pol = np.sum(pvec[(maskvec_dimhalf)])
            self.RingAsymPol = ((brightflux_pol_C/dimflux_pol_C),
                                brightflux_pol/dimflux_pol)

        # calculate dynamic range
        mask = self.im.copy()
        immask = self.im.copy()

        x0_c = mask.fovx()/2. - self.x0*ehc.RADPERUAS
        y0_c = self.y0*ehc.RADPERUAS - mask.fovy()/2.
        rad = self.RingSize1[0]*ehc.RADPERUAS

        mask.imvec *= 0
        mask = mask.add_gauss(1, [2*rad, 2*rad, 0, x0_c, y0_c])
        mask = mask.mask(cutoff=.5)
        maskvec = mask.imvec.astype(bool) + (immask.imvec < EP*self.flux)
        offsource_vec = immask.imvec[~(maskvec)]

        self.impeak = np.max(self.im.imvec)
        self.std_offsource = np.std(offsource_vec) + EP
        self.mean_offsource = np.mean(offsource_vec) + EP
        self.dynamic_range = self.impeak / self.std_offsource

    def calc_width(self, prof):
        pkrad, maxval = self.calc_pkrad_from_prof(prof)
        spline = scipy.interpolate.UnivariateSpline(self.rs, prof-0.5*maxval, s=0)
        roots = spline.roots()  # find the roots

        if len(roots) == 0:
            return(self.rs[0], self.rs[-1])

        lh = self.rs[0]
        rh = self.rs[-1]
        for root in np.sort(roots):
            if root < pkrad:
                lh = root
            else:
                rh = root
                break

        return (lh, rh)

    def calc_ringangle_asymmetry(self, prof):
        dtheta = self.thetas[-1]-self.thetas[-2]
        prof = prof / np.sum(prof*dtheta)  # normalize
        x = np.sum(prof * np.exp(1j*self.thetas) * dtheta)
        ang = np.mod(np.angle(x), 2*np.pi)
        asym = np.abs(x)
        std = np.sqrt(-2*np.log(np.abs(x)))
        return (ang, asym, std)

    def plot_img(self, postprocdir=POSTPROCDIR, save_png=False):
        plt.figure()
        plt.contour(self.xs, self.ys, self.imarr, colors='k')
        plt.xlabel(r"-RA ($\mu$as)")
        plt.ylabel(r"Dec ($\mu$as)")
        plt.plot(self.x0, self.y0, 'r*', markersize=20)

        for theta in np.linspace(0, 2*np.pi, 100):
            plt.plot(self.x0 + np.cos(theta) * self.RingSize1[0]/2,
                     self.y0 + np.sin(theta) * self.RingSize1[0]/2,
                     'r*', markersize=1)

        plt.axes().set_aspect('equal', 'datalim')

        if save_png:
            dirname = os.path.basename(os.path.dirname(self.imname))
            if not os.path.exists(postprocdir + '/' + dirname):
                subprocess.call(['mkdir', postprocdir + '/' + dirname])

            basename = os.path.basename(self.imname)
            fname = postprocdir + '/' + dirname + '/' + basename[:-5] + '_contour.png'

            plt.savefig(fname)
            plt.close()
        else:
            #plt.show(block=False)
            ehc.show_noblock()

    def plot_unwrapped(self, postprocdir=POSTPROCDIR, save_png=False,
                       xlabel=True, ylabel=True, xticklabel=True, yticklabel=True,
                       ax=False, imrange=[], show=True, cfun='jet', linecolor='r', labelsize=14):

        # line colors
        angcolor = np.array([100, 149, 237])/256.
        pkcolor = np.array([219, 0., 219])/256.
        # pkcolor = np.array([0,255,0])/256.

        imarr = np.array(self.profiles).T/1.e9
        if ax is False:
            plt.figure()
            ax = plt.gca()

        if imrange:
            plt.imshow(imarr, cmap=plt.get_cmap(cfun), origin='lower',
                       vmin=imrange[0], vmax=imrange[1], interpolation='gaussian')
        else:
            plt.imshow(imarr, cmap=plt.get_cmap(cfun), origin='lower', interpolation='gaussian')

        uas_to_pix = self.nrs/np.max(self.rs)  # convert radius to pixels
        rad_to_pix = self.nang/(2*np.pi)  # convert az. angle to pixels

        # horizontal lines -- radius
        pkloc = self.RingSize1[0]/2. * uas_to_pix
        lhloc = (self.RingSize1[0] - self.RingSize1[1])/2. * uas_to_pix
        rhloc = (self.RingSize1[0] + self.RingSize1[1]) / 2. * uas_to_pix

        plt.axhline(y=pkloc, color=linecolor, linewidth=1)
        plt.axhline(y=lhloc, color=linecolor, linewidth=1, linestyle=':')
        plt.axhline(y=rhloc, color=linecolor, linewidth=1, linestyle=':')

        # horizontal lines -- width
        # add radius and half width sigma in quadrature
        bandloc_sigma = np.sqrt((self.RingWidth[1]/2)**2 + (self.RingSize1[1]/2)**2)

        rhloc = (self.RingSize1[0]/2. + self.RingWidth[0]/2.) * uas_to_pix
        rhloc2 = (self.RingSize1[0]/2. + self.RingWidth[0]/2. + bandloc_sigma) * uas_to_pix
        rhloc3 = (self.RingSize1[0]/2. + self.RingWidth[0]/2. - bandloc_sigma) * uas_to_pix

        lhloc = (self.RingSize1[0]/2. - self.RingWidth[0]/2.) * uas_to_pix
        lhloc2 = (self.RingSize1[0]/2. - self.RingWidth[0]/2. + bandloc_sigma) * uas_to_pix
        lhloc3 = (self.RingSize1[0]/2. - self.RingWidth[0]/2. - bandloc_sigma) * uas_to_pix

        plt.axhline(y=lhloc, color=linecolor, linewidth=1, linestyle='--')
        plt.axhline(y=lhloc2, color=linecolor, linewidth=1, linestyle=':')
        plt.axhline(y=lhloc3, color=linecolor, linewidth=1, linestyle=':')
        plt.axhline(y=rhloc, color=linecolor, linewidth=1, linestyle='--')
        plt.axhline(y=rhloc2, color=linecolor, linewidth=1, linestyle=':')
        plt.axhline(y=rhloc3, color=linecolor, linewidth=1, linestyle=':')

        # position angle line
        pkloc = self.RingAngle1[0] * rad_to_pix
        lhloc = (self.RingAngle1[0] + self.RingAngle1[1]) * rad_to_pix
        rhloc = (self.RingAngle1[0] - self.RingAngle1[1]) * rad_to_pix

        plt.axvline(x=pkloc, color=angcolor, linewidth=1)
        plt.axvline(x=lhloc, color=angcolor, linewidth=1, linestyle=':')
        plt.axvline(x=rhloc, color=angcolor, linewidth=1, linestyle=':')

        # bright peak point
        plt.plot([self.abspk_loc_ang], [self.abspk_loc_rad], 'kx', mew=2, ms=6, color=pkcolor)

        # labels
        if xlabel:
            plt.xlabel(r"r$ \\theta $ ($^\circ$)", size=labelsize)
        if ylabel:
            plt.ylabel(r"r$r$ ($\mu$as)", size=labelsize)

        xticklabels = np.arange(0, 360, 60)
        xticks = (360/imarr.shape[1])*xticklabels

        yticks = np.floor(np.arange(0, imarr.shape[0], imarr.shape[0]/5)).astype(int)
        yticklabels = ["%0.0f" % r for r in self.rs[yticks]]

        if not xticklabel:
            xticklabels = []
        if not yticklabel:
            yticklabels = []

        plt.xticks(xticks, xticklabels)
        plt.yticks(yticks, yticklabels)
        plt.tick_params(axis='both', which='major', length=6)

        if save_png:
            dirname = os.path.basename(os.path.dirname(self.imname))
            if not os.path.exists(postprocdir + '/' + dirname):
                subprocess.call(['mkdir', postprocdir + '/' + dirname])

            basename = os.path.basename(self.imname)
            fname = postprocdir + '/' + dirname + '/' + basename[:-5] + '_unwrapped.png'
            plt.savefig(fname)
            plt.close()
        elif show:
            #plt.show(block=False)
            ehc.show_noblock()

    def save_unwrapped(self, fname):

        imarr = np.array(self.profiles).T

        header = fits.Header()
        header['CTYPE1'] = 'RA---SIN'
        header['CTYPE2'] = 'DEC--SIN'
        header['CDELT1'] = 2*np.pi/float(len(self.profiles))
        header['CDELT2'] = np.max(self.rs)/float(len(self.rs))
        header['BUNIT'] = 'K'
        hdu = fits.PrimaryHDU(imarr, header=header)
        hdulist = [hdu]
        hdulist = fits.HDUList(hdulist)
        hdulist.writeto(fname, overwrite=True)

    def plot_profs(self, postprocdir=POSTPROCDIR, save_png=False, colors=ehc.SCOLORS):
        plt.figure()
        plt.xlabel(r"distance from center ($\mu$as)")
        plt.ylabel(r"$T_{\rm b}$")
        plt.ylim([0, 1])
        plt.xlim([-10, 60])
        plt.title('All Profiles')
        for j in range(len(self.profiles)):
            plt.plot(self.rs, self.profiles[j], color=colors[j % len(colors)],
                     linestyle='-', linewidth=1)
        if save_png:
            dirname = os.path.basename(os.path.dirname(self.imname))
            if not os.path.exists(postprocdir + '/' + dirname):
                subprocess.call(['mkdir', postprocdir + '/' + dirname])

            basename = os.path.basename(self.imname)
            fname = postprocdir + '/' + dirname + '/' + basename[:-5] + '_profiles.png'

            plt.savefig(fname)
            plt.close()
        else:
            #plt.show(block=False)
            ehc.show_noblock()

    def plot_prof_band(self, postprocdir=POSTPROCDIR, save_png=False,
                       color='b', fontsize=14, show=True, axis=None, xlabel=True, ylabel=False):
        """2-sided plot of radial profiles, cut across orthogonal to position angle"""
        if axis is None:
            plt.figure()
            ax = plt.gca()
        else:
            ax = axis

        if xlabel:
            plt.xlabel(r"$r$ ($\mu$as)", size=fontsize)

        yticks = [0, 2, 4, 6, 8, 10]
        yticklabels = []
        if ylabel:
            plt.ylabel(r'Brightness Temperature ($10^9$ K)', size=fontsize)
            yticklabels = yticks

        plt.yticks(yticks, yticklabels)

        plt.ylim([0, 11])
        plt.xlim([-55, 55])

        # cut the ring in half orthagonal to the position angle
        cutloc1 = np.argmin(np.abs(self.thetas-np.mod(self.RingAngle1[0] - np.pi/2., 2*np.pi)))
        cutloc2 = np.argmin(np.abs(self.thetas-np.mod(self.RingAngle1[0] + np.pi/2., 2*np.pi)))

        if cutloc1 < cutloc2:
            prof_half_1 = self.profiles[cutloc1:cutloc2+1]
            prof_half_2 = np.vstack((self.profiles[cutloc2+1:], self.profiles[0:cutloc1]))
        else:
            prof_half_1 = np.vstack((self.profiles[cutloc1:], self.profiles[0:cutloc2+1]))
            prof_half_2 = self.profiles[cutloc2+1:cutloc1]

        # plot left half
        radii = -np.flip(self.rs)
        tho_m = np.flip(np.median(np.array(prof_half_1), axis=0))
        tho_l = np.flip(np.percentile(np.array(prof_half_1), 0, axis=0))
        tho_u = np.flip(np.percentile(np.array(prof_half_1), 100, axis=0))
        tho_l1 = np.flip(np.percentile(np.array(prof_half_1), 25, axis=0))
        tho_u1 = np.flip(np.percentile(np.array(prof_half_1), 75, axis=0))

        ax.plot(radii, tho_m/1.e9, 'b-', linewidth=2, color=color)
        ax.fill_between(radii, tho_l/1.e9, tho_u/1.e9, alpha=.2, edgecolor=None, facecolor=color)
        ax.fill_between(radii, tho_l1/1.e9, tho_u1/1.e9, alpha=.4, edgecolor=None, facecolor=color)

        # plot rights half
        radii = self.rs
        tho_m = np.median(np.array(prof_half_2), axis=0)
        tho_l = np.percentile(np.array(prof_half_2), 0, axis=0)
        tho_u = np.percentile(np.array(prof_half_2), 100, axis=0)
        tho_l1 = np.percentile(np.array(prof_half_2), 25, axis=0)
        tho_u1 = np.percentile(np.array(prof_half_2), 75, axis=0)

        ax.plot(radii, tho_m/1.e9, 'b-', linewidth=2, color=color)
        ax.fill_between(radii, tho_l/1.e9, tho_u/1.e9, alpha=.2, edgecolor=None, facecolor=color)
        ax.fill_between(radii, tho_l1/1.e9, tho_u1/1.e9, alpha=.4, edgecolor=None, facecolor=color)

        if save_png:
            dirname = os.path.basename(os.path.dirname(self.imname))
            if not os.path.exists(postprocdir + '/' + dirname):
                subprocess.call(['mkdir', postprocdir + '/' + dirname])

            basename = os.path.basename(self.imname)
            fname = postprocdir + '/' + dirname + '/' + basename[:-5] + '_band_profile.png'

            plt.savefig(fname)
            plt.close()
        if show:
            #plt.show(block=False)
            ehc.show_noblock()

    def plot_meanprof(self, postprocdir=POSTPROCDIR, save_png=False, color='k'):
        fig = plt.figure()
        plt.plot(self.rs, self.meanprof,
                 color=color, linestyle='-', linewidth=1)
        plt.plot((self.lh, self.rh), (0.5*self.meanpk, 0.5*self.meanpk),
                 color=color, linestyle='--', linewidth=1)
        plt.xlabel(r"distance from center ($\mu$as)")
        plt.ylabel(r"Flux (mJy/$\mu$as$^2$)")
        plt.ylim([0, 1])
        plt.xlim([-10, 60])
        plt.title('Mean Profile')
        if save_png:
            dirname = os.path.basename(os.path.dirname(self.imname))
            if not os.path.exists(postprocdir + '/' + dirname):
                subprocess.call(['mkdir', postprocdir + '/' + dirname])

            basename = os.path.basename(self.imname)
            fname = postprocdir + '/' + dirname + '/' + basename[:-5] + '_meanprofile.png'

            plt.savefig(fname)
            plt.close()
        else:
            #plt.show(block=False)
            ehc.show_noblock()

    def plot_meanprof_theta(self, postprocdir=POSTPROCDIR, save_png=False, color='k'):
        fig = plt.figure()
        plt.plot(self.thetas/ehc.DEGREE, self.meanprof_theta,
                 color=color, linestyle='-', linewidth=1)

        ang1 = self.RingAngle1[0]/ehc.DEGREE
        std1 = self.RingAngle1[1]/ehc.DEGREE
        up = np.mod(ang1+std1, 360)
        down = np.mod(ang1-std1, 360)
        plt.axvline(x=ang1, color='b', linewidth=1)
        plt.axvline(x=up, color='b', linewidth=1, linestyle='--')
        plt.axvline(x=down, color='b', linewidth=1, linestyle='--')

        ang2 = self.RingAngle2[0]/ehc.DEGREE
        std2 = self.RingAngle2[1]/ehc.DEGREE
        up = np.mod(ang2+std2, 360)
        down = np.mod(ang2-std2, 360)
        plt.axvline(x=ang2, color='r', linewidth=1)
        plt.axvline(x=up, color='r', linewidth=1, linestyle='--')
        plt.axvline(x=down, color='r', linewidth=1, linestyle='--')

        plt.xlabel(r"Angle E of N ($^{\circ}$)")
        plt.ylabel("Normalized Flux")
        plt.title('Mean Angular Profile')
        if save_png:
            dirname = os.path.basename(os.path.dirname(self.imname))
            if not os.path.exists(postprocdir + '/' + dirname):
                subprocess.call(['mkdir', postprocdir + '/' + dirname])

            basename = os.path.basename(self.imname)
            fname = postprocdir + '/' + dirname + '/' + basename[:-5] + '_meanangprofile.png'

            plt.savefig(fname)
            plt.close()
        else:
            #plt.show(block=False)
            ehc.show_noblock()

###################################################################################################
# Other functions
###################################################################################################


def quad_interp_radius(r_max, dr, val_list):
    v_L = val_list[0]
    v_max = val_list[1]
    v_R = val_list[2]

    rpk = r_max + dr*(v_L - v_R) / (2 * (v_L + v_R - 2*v_max))

    vpk = 8*v_max*(v_L + v_R) - (v_L - v_R)**2 - 16*v_max**2
    vpk /= (8*(v_L + v_R - 2*v_max))

    return (rpk, vpk)


def compute_ring_profile(im, x0, y0, title="",
                         nrays=NRAYS, nrs=NRS, rmin=RMIN, rmax=RMAX,
                         flux_norm=NORMFLUX, pol_profs=False):
    """compute a ring profile  given a center location
    """

    rs = np.linspace(0, rmax, nrs)
    thetas = np.linspace(0, 2*np.pi, nrays)

    factor = 3.254e13/(im.rf**2 * im.psize**2)  # convert to brightness temperature
    imarr = im.imvec.reshape(im.ydim, im.xdim)[::-1] * factor  # in brightness temperature K
    xs = np.arange(im.xdim)*im.psize/ehc.RADPERUAS
    ys = np.arange(im.ydim)*im.psize/ehc.RADPERUAS

    # TODO: test fiducial images with linear?
    interp = scipy.interpolate.interp2d(ys, xs, imarr, kind='cubic')

    def ringVals(theta):
        xxs = x0 - rs*np.sin(theta)
        yys = y0 + rs*np.cos(theta)

        vals = [interp(xxs[i], yys[i])[0] for i in np.arange(len(rs))]
        return vals

    profs = []
    for j in range(nrays):
        vals = ringVals(thetas[j])
        profs.append(vals)

    # polarization profiles
    profsQ = []
    profsU = []
    if len(im.qvec) > 0 and len(im.uvec > 0) and pol_profs:
        qarr = im.qvec.reshape(im.ydim, im.xdim)[::-1] * factor  # in brightness temperature K
        uarr = im.uvec.reshape(im.ydim, im.xdim)[::-1] * factor  # in brightness temperature K
        interpQ = scipy.interpolate.interp2d(ys, xs, qarr, kind='cubic')
        interpU = scipy.interpolate.interp2d(ys, xs, uarr, kind='cubic')

        def ringValsQ(theta):
            xxs = x0 - rs*np.sin(theta)
            yys = y0 + rs*np.cos(theta)

            vals = [interpQ(xxs[i], yys[i])[0] for i in np.arange(len(rs))]
            return vals

        def ringValsU(theta):
            xxs = x0 - rs*np.sin(theta)
            yys = y0 + rs*np.cos(theta)

            vals = [interpU(xxs[i], yys[i])[0] for i in np.arange(len(rs))]
            return vals

        for j in range(nrays):
            valsQ = ringValsQ(thetas[j])
            profsQ.append(valsQ)
            valsU = ringValsU(thetas[j])
            profsU.append(valsU)

    profiles = Profiles(im, x0, y0, profs, thetas, rmin=rmin, rmax=rmax, flux_norm=flux_norm,
                        profsQ=profsQ, profsU=profsU)

    return profiles


def findCenter(im,
               rmin=RMIN, rmax=RMAX,
               rmin_search=RPRIOR_MIN,  rmax_search=RPRIOR_MAX,
               nrays_search=NRAYS_SEARCH, nrs_search=NRS_SEARCH,
               fov_search=FOVP_SEARCH, n_search=NSEARCH, flux_norm=NORMFLUX):
    """Find the ring center by looking at profiles over a given range
    """

    print("nrays", nrays_search, "nrs", nrs_search, "fov", fov_search, "n", n_search)

    def objFunc(pos):
        (x0, y0) = pos
        profiles = compute_ring_profile(im, x0, y0, nrays=nrays_search, nrs=nrs_search,
                                        rmin=rmin, rmax=rmax, flux_norm=flux_norm)

        mean, std = profiles.RingSize1
        if mean < rmin_search or mean > rmax_search:
            return np.inf
        else:
            J = np.abs(std/mean)
            return J

    fovx = im.fovx()/ehc.RADPERUAS
    fovy = im.fovy()/ehc.RADPERUAS

    # brute force search + fmin finisher to find
    fovmin_x = (.5-fov_search) * fovx
    fovmax_x = (.5+fov_search) * fovx
    fovmin_y = (.5-fov_search) * fovy
    fovmax_y = (.5+fov_search) * fovy
    res = scipy.optimize.brute(objFunc, ranges=((fovmin_x, fovmax_x), (fovmin_y, fovmax_y)),
                               Ns=n_search)

    return res


def FindProfileSingle(imname, postprocdir,
                      save_files=False, blur=0, aipscc=False, tag='',
                      rerun=True, return_pp=True,
                      imsize=IMSIZE, npix=NPIX, rmin=RMIN, rmax=RMAX, nrays=NRAYS, nrs=NRS,
                      rmin_search=RPRIOR_MIN, rmax_search=RPRIOR_MAX,
                      nrays_search=NRAYS_SEARCH, nrs_search=NRS_SEARCH,
                      thresh_search=THRESH, fov_search=FOVP_SEARCH, n_search=NSEARCH,
                      flux_norm=NORMFLUX,center=False):
    """find the best ring profile for an image and save results
    """

    dirname = os.path.basename(os.path.dirname(imname))
    basename = os.path.basename(imname)
    txtname = postprocdir + '/' + dirname + '/' + basename[:-5] + tag + '.txt'
    if rerun is False and os.path.exists(txtname):
        return -1

    # print("nrays",nrays_search,"nrs",nrs_search,"fov",fov_search)
    with ploop.HiddenPrints():

        im_raw = load_image(imname, aipscc=aipscc)

        # blur image if requested
        if blur > 0:
            im_raw = im_raw.blur_circ(blur*ehc.RADPERUAS, blur*ehc.RADPERUAS)

        # center image and regrid to uniform pixel size and fox
        if center:
            im = di.center_core(im_raw) # TODO -- why isn't this working? 
        else:
            im = im_raw

        im_search = im.regrid_image(imsize, npix)
        im = im.regrid_image(imsize, npix)

        # blur image if requested
        # if blur > 0:
        #    im_search = im_search.blur_circ(blur*ehc.RADPERUAS)
        #    im = im.blur_circ(blur*ehc.RADPERUAS)

        # blur and threshold image FOR SEARCH ONLY
        # if blur==0:
        #    im_search = im.blur_circ(BLUR_VALUE_MIN*ehc.RADPERUAS)
        # else:
        #    im_search = im.copy()

        # threshold the search image to 5% of the maximum
        im_search.imvec[im_search.imvec < thresh_search*np.max(im_search.imvec)] = 0

        # find center
        res = findCenter(im_search, rmin=rmin, rmax=rmax,
                         rmin_search=rmin_search,  rmax_search=rmax_search,
                         nrays_search=nrays_search, nrs_search=nrs_search,
                         fov_search=fov_search, n_search=n_search, flux_norm=flux_norm)

        # compute profiles using the original (regridded, flux centroid centered) image
        print("compute profile")
        pp = compute_ring_profile(im, res[0], res[1], nrs=nrs, nrays=nrays,
                                  rmin=rmin, rmax=rmax, flux_norm=flux_norm,
                                  pol_profs=True)
        pp.calc_meanprof_and_stats()
        pp.imname = imname

        print("save files")
        if save_files:
            dirname = os.path.basename(os.path.dirname(imname))
            if not os.path.exists(postprocdir + '/' + dirname):
                subprocess.call(['mkdir', postprocdir + '/' + dirname])

            basename = os.path.splitext(os.path.basename(imname))[0]
            txtname = postprocdir + '/' + dirname + '/' + basename + tag + '.txt'

            if os.path.exists(txtname):
                os.remove(txtname)

            f = open(txtname, 'a')
            f.write('ring_x0 ' + str(res[0]) + '\n')
            f.write('ring_y0 ' + str(res[1]) + '\n')

            f.write('ring_diameter ' + str(pp.RingSize1[0]) + '\n')
            f.write('ring_diameter_sigma ' + str(pp.RingSize1[1]) + '\n')

            f.write('meanprof_ring_diameter ' + str(pp.RingSize2[0]) + '\n')
            f.write('meanprof_ring_diameter_sigma ' + str(pp.RingSize2[1]) + '\n')

            f.write('ring_orientation: ' + str(pp.RingAngle1[0]) + '\n')
            f.write('ring_orientation_sigma: ' + str(pp.RingAngle1[1]) + '\n')

            f.write('meanprof_ring_orientation: ' + str(pp.RingAngle2[0]) + '\n')
            f.write('meanprof_ring_orientation_sigma: ' + str(pp.RingAngle2[1]) + '\n')

            f.write('ring_width: ' + str(pp.RingWidth[0]) + '\n')
            f.write('ring_width_sigma: ' + str(pp.RingWidth[1]) + '\n')

            f.write('total_flux ' + str(pp.flux) + '\n')
            f.write('total_ring_flux ' + str(pp.RingFlux) + '\n')

            f.write('ring_asym_1 ' + str(pp.RingAsym1[0]) + '\n')
            f.write('ring_asym_1_sigma ' + str(pp.RingAsym1[1]) + '\n')
            f.write('ring_asym_2 ' + str(pp.RingAsym2[0]) + '\n')
            f.write('ring_brighthalf_over_dimhalf ' + str(pp.RingAsym2[1]) + '\n')

            f.write('in_flux_mean_ring ' + str(pp.in_level) + '\n')
            f.write('out_flux_mean_ring ' + str(pp.out_level) + '\n')
            f.write('max_flux_mean_ring ' + str(pp.meanpk) + '\n')

            f.write('max_ring_contrast: ' + str(pp.RingContrast1) + '\n')
            f.write('mean_ring_contrast: ' + str(pp.RingContrast2) + '\n')
            f.write('dynamic_range ' + str(pp.dynamic_range) + '\n')

            f.write('norm_factor ' + str(pp.normfactor) + '\n')

            f.write('ring_diameter_med ' + str(pp.RingSize1_med[0]) + '\n')
            f.write('ring_diameter_medabsdev ' + str(pp.RingSize1_med[1]) + '\n')

            f.write('ring_angle_pol ' + str(pp.RingAnglePol[0]) + '\n')
            f.write('ring_angle_pol_sigma ' + str(pp.RingAnglePol[1]) + '\n')

            f.write('ring_pol_ratio_p ' + str(pp.RingAsymPol[0]) + '\n')
            f.write('ring_pol_ratio_m ' + str(pp.RingAsymPol[1]) + '\n')

            f.close()

            #  save unwrapped and centered fits image
            # fitsname = postprocdir + '/' + dirname + '/' + basename[:-5] + tag + '.fits'
            # fitsname_centered = postprocdir + '/' + dirname + \
            #    '/' + basename[:-5] + tag + '_cent.fits'

            # pp.save_unwrapped(fitsname)
            # pp.im_center.save_fits(fitsname_centered)

            #  save radial profile
            # radprof_name = postprocdir + '/' + dirname + '/' + basename[:-5] + tag + '_radprof.txt
            # data=np.hstack((pp.rs.reshape(pp.nrs,1),
            #                pp.meanprof.reshape(pp.nrs,1),
            #                pp.normfactor * pp.meanprof.reshape(pp.nrs,1)))
            # np.savetxt(radprof_name, data)

            # save angular profile
            # angprof_name = postprocdir + '/' + dirname + '/' + basename[:-5] + tag+'_angprof.txt'
            # data=np.hstack((pp.thetas.reshape(pp.nthetas,1),
            #                pp.meanprof_theta.reshape(pp.nthetas,1),
            #                pp.normfactor * pp.meanprof_theta.reshape(pp.nthetas,1)))
            # np.savetxt(angprof_name, data)

            # pp.plot_unwrapped(save_png=True)
            # pp.plot_img(save_png=True)
            # pp.plot_meanprof(save_png=True)
            # pp.plot_meanprof_theta(save_png=True)
            # plt.close('all')

        if return_pp:
            return pp
        else:
            del pp
            return


def FindProfiles(foldername, postprocdir, processes=-1,
                 save_files=False, blur=0,
                 aipscc=False, tag='', rerun=True, return_pp=True,
                 imsize=IMSIZE, npix=NPIX, rmin=RMIN, rmax=RMAX, nrays=NRAYS, nrs=NRS,
                 rmin_search=RPRIOR_MIN, rmax_search=RPRIOR_MAX,
                 nrays_search=NRAYS_SEARCH, nrs_search=NRS_SEARCH,
                 thresh_search=THRESH, fov_search=FOVP_SEARCH, n_search=NSEARCH,
                 flux_norm=NORMFLUX
                 ):
    """find profiles for all images  in a directory
    """

    foldername = os.path.abspath(foldername)
    imlist = np.array(glob.glob(foldername + '/*.fits'))
    ext = '.fits'

    # Look for hdf5 files for EHT library runs
    if len(imlist) == 0:
        imlist = np.array(glob.glob(foldername + '/*.h5'))
        ext = '.h5'

    if len(imlist) == 0:
        print("\nfound no image files in ", foldername)
        return []

    print("\nfound ", len(imlist), "  ", ext, " files in ", foldername)

    imlist = np.sort(imlist)
    arglist = [[imlist[i], postprocdir,
                save_files, blur,
                aipscc, tag, rerun, return_pp,
                imsize, npix, rmin, rmax, nrays, nrs,
                rmin_search, rmax_search, nrays_search, nrs_search,
                thresh_search, fov_search, n_search, flux_norm]
               for i in range(len(imlist))]

    parloop = ploop.Parloop(FindProfileSingle)
    pplist = parloop.run_loop(arglist, processes)
    return pplist
