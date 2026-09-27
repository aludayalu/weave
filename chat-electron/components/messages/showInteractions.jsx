import { Spinner } from "@heroui/react"
import { AssistantInteraction } from "./assistantInteraction"
import { UserInteraction } from "./userInteraction"

export function Interactions({interactions, showSpinner, container_ref}) {
    return (
        <div className="flex flex-col gap-5 pt-4 pb-[300px] min-w-0 w-full" ref={container_ref}>
            {interactions.map((interaction, i) => {
                if (interaction.side == "user") {
                    return <UserInteraction key={interaction.id} interaction={interaction} />
                } else {
                    return <AssistantInteraction key={interaction.id} interaction={interaction} showSpinner={showSpinner} />
                }
            })}

            {showSpinner && <Spinner color="success"></Spinner>}
        </div>
    )
}