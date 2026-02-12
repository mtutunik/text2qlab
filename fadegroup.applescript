set userUpTime to 0 -- Time for images to fade up in
set userDownTime to 0 -- Time for images to fade down in
set userHoldTime to 0.1 -- Time to wait between starting fade down and starting fade up
-- Declarations
set notFirstCue to false

-- Main routine

tell application id "com.figure53.QLab.5" to tell front workspace
	set selectedCues to every cue as list
	repeat with eachcue in selectedCues
		if q type of eachcue is "Text" then -- Any other selected cues will be ignored; the sequence will end up after them
			
			-- Make a fade out cue (not for the first cue)
			if notFirstCue then
				make type "Fade" -- Cue numbers and names not altered from QLab defaults
				set fadeOutCue to last item of (selected as list)
				set cue target of fadeOutCue to previousCue
				set duration of fadeOutCue to userDownTime
				set do opacity of fadeOutCue to true
				set opacity of fadeOutCue to 0
				set stop target when done of fadeOutCue to true
			end if
			
			-- Make a fade in cue
			make type "Fade" -- Cue numbers and names not altered from QLab defaults
			set fadeInCue to last item of (selected as list)
			set opacity of eachcue to 0
			set cue target of fadeInCue to eachcue
			set pre wait of fadeInCue to userHoldTime
			set duration of fadeInCue to userUpTime
			set do opacity of fadeInCue to true
			
			-- Make a Group Cue (have to do this here to get round QLab's auto-grouping of selections ≥ 2 cues)
			make type "Group" -- Cue numbers not altered from QLab defaults
			set groupCue to last item of (selected as list)
			set mode of groupCue to timeline
			set q name of groupCue to "Crossfade to " & q list name of eachcue
			
			-- Move cues into right place
			
			move cue id (uniqueID of eachcue) of parent of eachcue to end of groupCue
			if notFirstCue then move cue id (uniqueID of fadeOutCue) of parent of fadeOutCue to end of groupCue
			move cue id (uniqueID of fadeInCue) of parent of fadeInCue to end of groupCue
			
			-- Setup variables for next pass
			set previousCue to eachcue
			set notFirstCue to true
		end if
	end repeat
end tell
